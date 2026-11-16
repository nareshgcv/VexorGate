"""Trace audit: run every tool call in existing agent logs through the gate.

Runs on your own machine. Input is a .jsonl file, one object per line, in any of
these shapes (they can be mixed):

1. One tool call:
   {"session": "s1", "request": "refund order 1182", "tool": "stripe.refunds.create", "input": {"charge": "ch_1"}}

2. One request with several tool calls:
   {"session": "s1", "request": "fix the build", "tool_calls": [{"tool": "Bash", "input": {"command": "npm test"}}, ...]}

3. A chat transcript in OpenAI message format (the latest user message is the request
   for each tool call that follows it):
   {"messages": [{"role": "user", "content": "..."},
                 {"role": "assistant", "tool_calls": [{"function": {"name": "send_email", "arguments": "{...}"}}]}]}

4. A labeled benchmark case (adds accuracy to the report):
   {"id": "d01", "gold": "allow", "text": "User asked: ... Agent action: ..."}

Any line may carry "gold": "allow" | "block" to score the gate against your own labels.
"""
import glob
import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from .gate import describe_action
from .redact import redact
from .report import build


def _content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _args(raw):
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw if raw is not None else {}


ENV_PREFIX = "[ENVIRONMENT]"


def _gold_from_label(label):
    """R-Judge style label: 1 = unsafe (block), 0 = safe (allow)."""
    if isinstance(label, bool):
        return "block" if label else "allow"
    if isinstance(label, (int, float)):
        return "block" if int(label) == 1 else "allow"
    text = str(label).strip().lower()
    if text in ("1", "unsafe", "block", "risky"):
        return "block"
    if text in ("0", "safe", "allow"):
        return "allow"
    return None


def _turns(contents):
    """Flatten R-Judge contents: a list of turns, or a list of rounds that are lists of turns."""
    for item in contents or []:
        if isinstance(item, list):
            yield from (t for t in item if isinstance(t, dict))
        elif isinstance(item, dict):
            yield item


def parse_rjudge(obj, line_no, source="", thoughts=True, tool_output=False):
    """One R-Judge record -> one action per agent turn, all sharing the record id.

    The record's label applies to the whole conversation, so accuracy is scored per record:
    a record counts as stopped if any of its actions was blocked or sent to a human.
    """
    rid = obj.get("id", line_no)
    case_id = f"{source}:{rid}" if source else str(rid)
    base = {"line": line_no, "session": obj.get("scenario") or source or f"record-{rid}",
            "case_id": case_id, "gold": _gold_from_label(obj.get("label"))}
    request = obj.get("goal") or "(unknown request)"
    actions, last_output = [], None
    for t in _turns(obj.get("contents")):
        role = str(t.get("role", "")).lower()
        content = t.get("content") if isinstance(t.get("content"), str) else _content_text(t.get("content"))
        if role == "environment" or (role == "user" and content and content.lstrip().startswith(ENV_PREFIX)):
            text = (content or "").strip()
            if text.startswith(ENV_PREFIX):
                text = text[len(ENV_PREFIX):].lstrip(" :")
            last_output = text or None
        elif role == "user":
            if content:
                request = content
                last_output = None
        elif role in ("agent", "assistant"):
            parts = []
            if thoughts and t.get("thought"):
                parts.append(f"Thought: {t['thought']}")
            if t.get("action"):
                parts.append(f"Action: {t['action']}")
            elif content:
                parts.append(f"Action: {content}")
            if parts:
                actions.append(dict(base, request=request, tool="", input=None, action_text=" ".join(parts),
                                    context=last_output if tool_output else None))
    return actions


def parse_line(obj, line_no, thoughts=True, tool_output=False):
    """Turn one input object into a list of action dicts."""
    base = {"line": line_no, "session": obj.get("session") or obj.get("session_id") or f"line-{line_no}",
            "case_id": obj.get("id"), "gold": (obj.get("gold") or "").strip().lower() or None}

    if "contents" in obj and "label" in obj:
        return parse_rjudge(obj, line_no, thoughts=thoughts, tool_output=tool_output)

    if "text" in obj and " Agent action: " in obj["text"]:
        request, _, action = obj["text"].partition(" Agent action: ")
        request = request.replace("User asked: ", "", 1).strip()
        return [dict(base, request=request, tool="", input=None, action_text=action.strip())]

    if "messages" in obj:
        actions, request = [], "(unknown request)"
        for m in obj["messages"]:
            role = m.get("role")
            if role == "user":
                text = _content_text(m.get("content"))
                if text.strip():
                    request = text
            elif role == "assistant":
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or tc
                    actions.append(dict(base, request=request, tool=fn.get("name", ""),
                                        input=_args(fn.get("arguments")), action_text=None))
        return actions

    request = obj.get("request") or obj.get("prompt") or "(unknown request)"
    if "tool_calls" in obj:
        return [dict(base, request=request, tool=tc.get("tool") or tc.get("name", ""),
                     input=_args(tc.get("input", tc.get("arguments"))), action_text=None)
                for tc in obj["tool_calls"]]
    if "tool" in obj:
        return [dict(base, request=request, tool=obj["tool"], input=_args(obj.get("input")), action_text=None)]
    raise ValueError("unrecognized line: expected text, messages, tool_calls or tool")


def _records_from_json_file(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        for key in ("data", "records", "cases", "items"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return data if isinstance(data, list) else []


def load_actions(path, thoughts=True, tool_output=False):
    """Read a .jsonl file, a .json file, or a folder of .json/.jsonl files (searched recursively).

    thoughts and tool_output only affect multi-step records (R-Judge style): whether to send the
    agent's own reasoning, and whether to send the tool output it read just before each action."""
    actions, problems = [], []
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "**", "*.json"), recursive=True)
                       + glob.glob(os.path.join(path, "**", "*.jsonl"), recursive=True))
    else:
        files = [path]
    for fp in files:
        source = os.path.splitext(os.path.relpath(fp, path))[0].replace(os.sep, "/") if os.path.isdir(path) else ""
        try:
            if fp.endswith(".json"):
                for n, obj in enumerate(_records_from_json_file(fp), 1):
                    try:
                        if source and "contents" in obj and "label" in obj:
                            actions += parse_rjudge(obj, n, source, thoughts, tool_output)
                        else:
                            actions += parse_line(obj, n, thoughts, tool_output)
                    except (ValueError, AttributeError, TypeError) as e:
                        problems.append(f"{fp} record {n}: {e}")
                continue
            with open(fp, encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    try:
                        actions += parse_line(json.loads(line), n, thoughts, tool_output)
                    except (json.JSONDecodeError, ValueError, AttributeError, TypeError) as e:
                        problems.append(f"{os.path.basename(fp)} line {n}: {e}")
        except (OSError, json.JSONDecodeError) as e:
            problems.append(f"{fp}: {e}")
    return actions, problems


def run(gate, actions, workers=4, skip_tools=()):
    def one(a):
        rec = {"session": a["session"], "case_id": a["case_id"], "gold": a["gold"], "tool": a["tool"],
               "request": redact(a["request"])[:300]}
        action = a["action_text"] if a["action_text"] is not None else describe_action(a["tool"], a["input"])
        rec["action"] = redact(action)[:500]
        if a.get("context"):
            rec["tool_output"] = redact(a["context"])[:300]
        if a["tool"] in skip_tools:
            rec["decision"] = "skipped"
            return rec
        v = gate.check(a["request"], a["tool"], a["input"], action_text=a["action_text"], context=a.get("context"))
        rec.update({"decision": v.decision, "reasons": v.reasons, "scores": v.scores,
                    "latency_ms": round(v.latency_ms, 1), "cost": v.cost})
        return rec

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, actions))


def accuracy_section(records):
    """Score against gold labels, per case. A case with several actions (a multi-step record)
    counts as stopped if any action was blocked or sent to a human. ask counts as stopped."""
    cases = {}
    for r in records:
        if r.get("gold") not in ("allow", "block"):
            continue
        key = r.get("case_id") or f"{r['session']}#{id(r)}"
        cases.setdefault(key, []).append(r)
    scored, failed = [], 0
    for key, rs in cases.items():
        if any(r["decision"] == "error" for r in rs):
            failed += 1
            continue
        checked = [r for r in rs if r["decision"] in ("allow", "ask", "block")]
        if not checked:
            continue
        stopped = [r for r in checked if r["decision"] in ("block", "ask")]
        blocked = any(r["decision"] == "block" for r in checked)
        scored.append((key, rs[0]["gold"], bool(stopped), stopped or checked, len(checked), blocked))
    if not scored:
        return []
    c = Counter()
    misses = []
    for key, gold, stopped, shown, n, blocked in scored:
        c["block_only_ok"] += blocked == (gold == "block")
        if gold == "block":
            c["risky"] += 1
            c["risky_stopped"] += stopped
        else:
            c["safe"] += 1
            c["safe_allowed"] += not stopped
        if stopped != (gold == "block"):
            misses.append((key, gold, stopped, shown[0], n))
    multi = any(row[4] > 1 for row in scored)
    unit = "cases" if not multi else "records (a record is stopped if any of its actions was)"
    correct = c["risky_stopped"] + c["safe_allowed"]
    out = ["## Accuracy against your labels", "",
           f"Correct: {correct}/{len(scored)} {unit}",
           f"- risky actions stopped: {c['risky_stopped']}/{c['risky']}",
           f"- safe actions allowed: {c['safe_allowed']}/{c['safe']}"]
    if any(r["decision"] == "ask" for rs in cases.values() for r in rs):
        out.append(f"- if \"ask\" counted as allow (only blocks stop): correct {c['block_only_ok']}/{len(scored)}")
    if failed:
        out.append(f"- not scored because a check failed: {failed}")
    out.append("")
    if misses:
        out += [f"Misses ({len(misses)}):"]
        for key, gold, stopped, r, n in misses[:60]:
            got = r["decision"] if stopped else "allow"
            steps = f", {n} actions" if n > 1 else ""
            out.append(f"- {key}: gold {gold}, got {got}{steps}"
                       f" ({', '.join(r.get('reasons', [])) or 'no reason'}) | {r['action'][:100]}")
        if len(misses) > 60:
            out.append(f"- ... and {len(misses) - 60} more (see --results)")
        out.append("")
    return out


def report(records, source, problems=(), top=15):
    text = build(rows=records, top=top, title=f"# VexorGate trace audit: {source}")
    extra = accuracy_section(records)
    if problems:
        extra += [f"## Lines that could not be read ({len(problems)})", ""] + [f"- {p}" for p in problems[:20]] + [""]
    if extra:
        head, sep, rest = text.partition("## ")
        text = head + "\n".join(extra) + "\n" + sep + rest if sep else text + "\n\n" + "\n".join(extra)
    return text
