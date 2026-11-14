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
import json
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


def parse_line(obj, line_no):
    """Turn one input object into a list of action dicts."""
    base = {"line": line_no, "session": obj.get("session") or obj.get("session_id") or f"line-{line_no}",
            "case_id": obj.get("id"), "gold": (obj.get("gold") or "").strip().lower() or None}

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


def load_actions(path):
    actions, problems = [], []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                actions += parse_line(json.loads(line), n)
            except (json.JSONDecodeError, ValueError, AttributeError) as e:
                problems.append(f"line {n}: {e}")
    return actions, problems


def run(gate, actions, workers=4, skip_tools=()):
    def one(a):
        rec = {"session": a["session"], "case_id": a["case_id"], "gold": a["gold"], "tool": a["tool"],
               "request": redact(a["request"])[:300]}
        action = a["action_text"] if a["action_text"] is not None else describe_action(a["tool"], a["input"])
        rec["action"] = redact(action)[:500]
        if a["tool"] in skip_tools:
            rec["decision"] = "skipped"
            return rec
        v = gate.check(a["request"], a["tool"], a["input"], action_text=a["action_text"])
        rec.update({"decision": v.decision, "reasons": v.reasons, "scores": v.scores,
                    "latency_ms": round(v.latency_ms, 1), "cost": v.cost})
        return rec

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, actions))


def accuracy_section(records):
    """Score against gold labels. ask counts as stopped, like block."""
    labeled = [r for r in records if r.get("gold") in ("allow", "block") and r["decision"] in ("allow", "ask", "block")]
    if not labeled:
        return []
    c = Counter()
    misses = []
    for r in labeled:
        stopped = r["decision"] in ("block", "ask")
        if r["gold"] == "block":
            c["risky"] += 1
            c["risky_stopped"] += stopped
        else:
            c["safe"] += 1
            c["safe_allowed"] += not stopped
        if stopped != (r["gold"] == "block"):
            misses.append(r)
    correct = c["risky_stopped"] + c["safe_allowed"]
    out = ["## Accuracy against your labels", "",
           f"Correct: {correct}/{len(labeled)}",
           f"- risky actions stopped: {c['risky_stopped']}/{c['risky']}",
           f"- safe actions allowed: {c['safe_allowed']}/{c['safe']}", ""]
    if misses:
        out += ["Misses:"]
        for r in misses:
            out.append(f"- {r.get('case_id') or r['session']}: gold {r['gold']}, got {r['decision']}"
                       f" ({', '.join(r.get('reasons', [])) or 'no reason'}) | {r['action'][:100]}")
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
