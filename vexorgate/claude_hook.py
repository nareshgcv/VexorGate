"""Claude Code hook for VexorGate.

Register it for UserPromptSubmit and PreToolUse (see docs/claude-code.md).
  UserPromptSubmit: remembers the user's request for this session.
  PreToolUse:       checks the tool call against that request and logs the verdict.
In shadow mode (default) it never interferes. In enforce mode it answers
"deny" for block and "ask" for ask or error, so the user decides.
The hook always exits 0 and never raises, so a failure cannot break Claude Code.
"""
import json
import re
import sys
import time
from datetime import datetime, timezone

from . import config
from .gate import describe_action, gate_from_config
from .redact import redact


def _session_file(session_id):
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", session_id or "unknown")[:80]
    d = config.home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{safe}.json"


def _load_session(session_id):
    f = _session_file(session_id)
    return json.loads(f.read_text()) if f.exists() else {}


def _save_session(session_id, data):
    _session_file(session_id).write_text(json.dumps(data))


def remember_prompt(session_id, prompt):
    data = _load_session(session_id)
    data["prompts"] = (data.get("prompts", []) + [prompt])[-5:]
    _save_session(session_id, data)


def last_prompt(session_id):
    f = _session_file(session_id)
    if f.exists():
        prompts = json.loads(f.read_text()).get("prompts", [])
        if prompts:
            return prompts[-1]
    return None


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
    d = config.home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(d / f"{day}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def enforce_output(decision, reasons, gap=""):
    if decision == "block":
        permission = "deny"
        why = "VexorGate blocked this action: " + (gap or ", ".join(reasons))
    elif decision == "ask":
        permission = "ask"
        why = "VexorGate wants confirmation: " + (gap or ", ".join(reasons))
    else:  # error
        permission = "ask"
        why = "VexorGate could not check this action, please confirm"
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": permission,
                                   "permissionDecisionReason": why}}


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

    request = last_prompt(session) or prompt_from_transcript(data.get("transcript_path")) or "(unknown request)"
    session_data = _load_session(session)
    ledger = session_data.setdefault("ledger", {})
    if gate is None:
        gate = gate_from_config(cfg, ledger)
    else:
        gate.ledger.state = ledger
    v = gate.check(request, tool, tool_input, session=session)
    _save_session(session, session_data)
    record.update({
        "request": redact(request)[:300],
        "action": redact(describe_action(tool, tool_input))[:500],
        "decision": v.decision, "reasons": v.reasons, "scores": v.scores,
        "latency_ms": round(v.latency_ms, 1), "cost": v.cost,
    })
    if v.gap:
        record["gap"] = v.gap
    log(record)

    if cfg["mode"] == "enforce" and v.decision in ("block", "ask", "error"):
        return enforce_output(v.decision, v.reasons, v.gap)
    return None


def main():
    try:
        data = json.load(sys.stdin)
        out = handle(data)
        if out:
            print(json.dumps(out))
    except Exception as e:
        try:
            log({"ts": time.time(), "decision": "hook_error", "error": f"{type(e).__name__}: {str(e)[:200]}"})
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
