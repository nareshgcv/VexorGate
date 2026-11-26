"""Claude Code hook for VexorGate.

Register it for UserPromptSubmit and PreToolUse, and optionally PostToolUse (see docs/claude-code.md).
  UserPromptSubmit: remembers the user's request for this session.
  PreToolUse:       checks the tool call against that request and logs the verdict.
  PostToolUse:      records whether the last money action went through or failed, so a failed
                    call is released and a corrected retry is not counted twice.
In shadow mode (default) it never interferes. In enforce mode it answers
"deny" for block and "ask" for ask or error, so the user decides.

It fails closed: in enforce mode, any problem inside the hook (a damaged session file, a broken
config, an unexpected input, a lock it cannot get) answers "ask", never silence, because Claude
Code runs a tool when its hook gives no answer. The hook always exits 0 and never raises.

Session files and logs are private to the user (folder 0700, files 0600 where the system allows),
prompts are redacted before they are stored, files are written atomically, and a per-session lock
keeps parallel tool calls from losing each other's counts.
"""
import contextlib
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone

from . import config
from .facts import Ledger
from .gate import describe_action, gate_from_config
from .redact import redact


LOCK_WAIT = 10.0    # seconds to wait for another hook working on the same session


def _private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    for d in (config.home(), path):
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    return path


def _write_private(path, text):
    """Write atomically (temporary file, then rename) with owner-only permissions."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _session_file(session_id):
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", session_id or "unknown")[:80]
    return _private_dir(config.home() / "sessions") / f"{safe}.json"


@contextlib.contextmanager
def _session_lock(session_id, wait=None):
    """One hook at a time per session. Raises TimeoutError if the lock is not free in time."""
    path = _session_file(session_id).with_suffix(".lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + (LOCK_WAIT if wait is None else wait)
    try:
        try:
            import fcntl

            def take():
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def free():
                fcntl.flock(fd, fcntl.LOCK_UN)
        except ImportError:                       # Windows
            import msvcrt

            def take():
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

            def free():
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        while True:
            try:
                take()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError("another VexorGate hook kept this session busy") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            free()
    finally:
        os.close(fd)


def _load_session(session_id):
    """The saved session, or {} if there is none. A damaged file is set aside and reported
    with "_damaged", so earlier counts are known to be lost."""
    f = _session_file(session_id)
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data
    except (ValueError, OSError):
        with contextlib.suppress(OSError):
            os.replace(f, f.with_name(f"{f.stem}.damaged-{int(time.time())}.json"))
        return {"_damaged": True}


def _save_session(session_id, data):
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    _write_private(_session_file(session_id), json.dumps(data))


def remember_prompt(session_id, prompt):
    with _session_lock(session_id):
        data = _load_session(session_id)
        data["prompts"] = (data.get("prompts", []) + [redact(prompt)])[-5:]
        _save_session(session_id, data)


def last_prompt(session_id):
    prompts = _load_session(session_id).get("prompts") or []
    return prompts[-1] if prompts else None


def prompt_from_transcript(path):
    """Fallback: the last real user message in the Claude Code transcript (.jsonl)."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, TypeError):
        return None
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "user":
            continue
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue
            texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            if any(t.strip() for t in texts):
                return " ".join(texts)
    return None


def log(record):
    d = _private_dir(config.home() / "logs")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fd = os.open(d / f"{day}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def enforce_output(decision, reasons, gap="", receipt="", detail=""):
    exact = f" Exact action: {receipt}" if receipt else ""
    if decision == "block":
        permission = "deny"
        why = "VexorGate blocked this action: " + (gap or ", ".join(reasons)) + exact
    elif decision == "ask":
        permission = "ask"
        why = "VexorGate wants confirmation: " + (gap or ", ".join(reasons)) + exact
    else:  # error
        permission = "ask"
        why = "VexorGate could not check this action" + (f" ({detail})" if detail else "") + ", please confirm"
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": permission,
                                   "permissionDecisionReason": why}}


def _looks_failed(response):
    if isinstance(response, dict):
        if response.get("is_error") or response.get("isError") or response.get("error"):
            return True
        status = str(response.get("status", "")).lower()
        return status in ("failed", "error", "declined", "rejected")
    return False


def handle(data, gate=None, cfg=None):
    """Process one hook event. Returns the JSON to print, or None."""
    cfg = cfg or config.load()
    event = data.get("hook_event_name")
    session = data.get("session_id", "unknown")

    if event == "UserPromptSubmit":
        prompt = data.get("prompt")
        if prompt:
            remember_prompt(session, prompt)
        return None

    if event in ("PostToolUse", "PostToolUseFailure"):
        with _session_lock(session):
            data_ = _load_session(session)
            if "ledger" in data_:
                failed = event == "PostToolUseFailure" or _looks_failed(data.get("tool_response"))
                Ledger(data_["ledger"]).settle(session, "failed" if failed else "ok")
                _save_session(session, data_)
        return None

    if event != "PreToolUse":
        return None

    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "session": session,
              "tool": tool, "mode": cfg["mode"]}

    if tool in cfg.get("skip_tools", []):
        record["decision"] = "skipped"
        log(record)
        return None

    with _session_lock(session):
        session_data = _load_session(session)
        damaged = session_data.pop("_damaged", False)
        prompts = session_data.get("prompts") or []
        transcript = prompt_from_transcript(data.get("transcript_path")) if not prompts else None
        request = prompts[-1] if prompts else (redact(transcript) if transcript else "(unknown request)")
        ledger = session_data.setdefault("ledger", {})
        if gate is None:
            gate = gate_from_config(cfg, ledger)
        else:
            gate.ledger.state = ledger
        v = gate.check(request, tool, tool_input, session=session)
        _save_session(session, session_data)
    if damaged and v.decision in ("allow", "ask"):
        v.decision = "ask"
        v.reasons = ["session_damaged"] + list(v.reasons)
        v.gap = ("VexorGate's saved session was damaged, so earlier counts in this session were lost. "
                 + (v.gap or "")).strip()
    record.update({
        "request": redact(request)[:300],
        "action": redact(describe_action(tool, tool_input))[:500],
        "decision": v.decision, "reasons": v.reasons, "scores": v.scores,
        "latency_ms": round(v.latency_ms, 1), "cost": v.cost,
    })
    if v.gap:
        record["gap"] = v.gap
    if v.receipt:
        record["receipt"] = v.receipt
    log(record)

    if cfg["mode"] == "enforce" and v.decision in ("block", "ask", "error"):
        return enforce_output(v.decision, v.reasons, v.gap, v.receipt)
    return None


def _enforcing():
    """Whether enforce mode is on, even when the config file cannot be read."""
    try:
        return config.load().get("mode") == "enforce"
    except Exception:
        pass
    if (os.environ.get("VEXORGATE_MODE") or "").strip().lower() == "enforce":
        return True
    try:
        raw = (config.home() / "config.json").read_text(encoding="utf-8", errors="replace")
        return bool(re.search(r'"mode"\s*:\s*"enforce"', raw))
    except OSError:
        return False


def main():
    data = None
    try:
        data = json.load(sys.stdin)
        out = handle(data)
        if out:
            print(json.dumps(out))
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:200]}"
        with contextlib.suppress(Exception):
            log({"ts": time.time(), "decision": "hook_error", "error": error})
        # fail closed: Claude Code runs the tool when the hook gives no answer
        event = data.get("hook_event_name") if isinstance(data, dict) else None
        with contextlib.suppress(Exception):
            if event in (None, "PreToolUse") and _enforcing():
                print(json.dumps(enforce_output("error", [], detail=f"internal error: {type(e).__name__}")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
