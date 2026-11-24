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

Any line may carry "gold": "allow" | "block" to score the gate against your own labels, per case.
To score each call, put "gold" on the tool call itself, or give the record "gold_calls": ["block", "allow", ...].
Any line may carry "org": "acme.com" to set the organization domain for that record.

Tool results are kept. In the messages format they come from "tool" messages (or tool_result blocks);
in the tool_calls format from a "result" or "output" field on each call. They are used to:
  - mark calls whose result is missing ("couldn't verify", with the reason), and
  - tell the gate whether a money action went through, failed, or has an unknown outcome
    (a timeout or no response), so a retry after a failure is not counted twice and a new
    order after a lost response is sent to a person.
"""
import glob
import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from .gate import describe_action
from .redact import redact
from .report import build

VERIFY_REASONS = {
    "no_result": "no result was logged for this call",
    "log_ends": "the log ends right after this call (crash or cut-off log)",
    "empty_result": "the result is empty, so only the arguments were judged",
    "unreadable": "the record could not be read",
}

UNKNOWN_OUTCOME = re.compile(r"time[d\s-]*out|no response|connection (reset|closed|aborted|refused)|gateway timeout|"
                             r"\b504\b|unknown status|status unknown|did not respond|network error", re.I)
FAILED_OUTCOME = re.compile(r"\b(error|failed|failure|declined|rejected|invalid|denied|not found|insufficient|"
                            r"exception|unauthori[sz]ed|forbidden|cancell?ed|blocked)\b", re.I)


def outcome_of(result):
    """What happened to a call, from its logged result: ok, failed or unknown."""
    if result is None:
        return "unknown"
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    if not text.strip():
        return "unknown"
    if UNKNOWN_OUTCOME.search(text):
        return "unknown"
    if FAILED_OUTCOME.search(text):
        return "failed"
    return "ok"


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
    if obj.get("org"):
        base["org"] = str(obj["org"]).strip().lower()
    gold_calls = [str(g).strip().lower() for g in obj.get("gold_calls") or []]

    if "contents" in obj and "label" in obj:
        return parse_rjudge(obj, line_no, thoughts=thoughts, tool_output=tool_output)

    if "text" in obj and " Agent action: " in obj["text"]:
        request, _, action = obj["text"].partition(" Agent action: ")
        request = request.replace("User asked: ", "", 1).strip()
        return [dict(base, request=request, tool="", input=None, action_text=action.strip())]

    if "messages" in obj:
        actions = _parse_messages(obj["messages"], base, tool_output)
        return _apply_gold_calls(actions, gold_calls)

    request = obj.get("request") or obj.get("prompt") or "(unknown request)"
    if "tool_calls" in obj:
        calls = obj["tool_calls"]
        logged = any(isinstance(tc, dict) and any(k in tc for k in ("result", "output", "outcome")) for tc in calls)
        actions, last_output = [], None
        for i, tc in enumerate(calls):
            a = dict(base, request=request, tool=tc.get("tool") or tc.get("name", ""),
                     input=_args(tc.get("input", tc.get("arguments"))), action_text=None,
                     context=last_output if tool_output else None)
            if tc.get("gold"):
                a["gold_call"] = str(tc["gold"]).strip().lower()
            if logged:
                result = tc.get("result", tc.get("output"))
                a["outcome"] = tc.get("outcome") or outcome_of(result)
                if result is None and not tc.get("outcome"):
                    a["verify"] = "log_ends" if i == len(calls) - 1 else "no_result"
                elif isinstance(result, str) and not result.strip():
                    a["verify"] = "empty_result"
                last_output = _result_text(result)
            actions.append(a)
        return _apply_gold_calls(actions, gold_calls)
    if "tool" in obj:
        a = dict(base, request=request, tool=obj["tool"], input=_args(obj.get("input")), action_text=None)
        if "outcome" in obj or "result" in obj:
            a["outcome"] = obj.get("outcome") or outcome_of(obj.get("result"))
        return _apply_gold_calls([a], gold_calls)
    raise ValueError("unrecognized line: expected text, messages, tool_calls or tool")


def _result_text(result):
    if result is None:
        return None
    if isinstance(result, list):
        result = _content_text(result) or json.dumps(result, ensure_ascii=False)
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


def _apply_gold_calls(actions, gold_calls):
    for a, g in zip(actions, gold_calls):
        if g in ("allow", "block", "ask"):
            a["gold_call"] = g
    return actions


def _parse_messages(messages, base, tool_output=False):
    """OpenAI or Anthropic style messages. Each tool call is judged against the latest user
    message, and paired with its result (a "tool" message or a tool_result block)."""
    calls, results = [], {}
    request, last_output = "(unknown request)", None
    for pos, m in enumerate(messages):
        role = m.get("role")
        content = m.get("content")
        if role == "tool":
            results[m.get("tool_call_id") or f"#pos{pos}"] = (pos, _result_text(content))
            last_output = _result_text(content)
            continue
        if role == "user":
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        results[b.get("tool_use_id") or f"#pos{pos}"] = (pos, _result_text(b.get("content")))
                        last_output = _result_text(b.get("content"))
            text = _content_text(content)
            if text.strip():
                request = text
                last_output = None
        elif role == "assistant":
            blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"] \
                if isinstance(content, list) else []
            for tc in (m.get("tool_calls") or []) + blocks:
                fn = tc.get("function") or tc
                a = dict(base, request=request, tool=fn.get("name", ""),
                         input=_args(fn.get("arguments", fn.get("input"))), action_text=None,
                         context=last_output if tool_output else None)
                if tc.get("gold") or m.get("gold"):
                    a["gold_call"] = str(tc.get("gold") or m.get("gold")).strip().lower()
                calls.append((pos, tc.get("id"), a))
    last_pos = len(messages) - 1
    unpaired = [r for r in results if r.startswith("#pos")]
    out = []
    for pos, cid, a in calls:
        hit = results.get(cid) if cid else None
        if hit is None and unpaired:        # results without ids: pair in order
            hit = results.pop(unpaired.pop(0))
        if results or hit is not None or any(m.get("role") == "tool" for m in messages):
            if hit is None:
                a["outcome"] = "unknown"
                later = any(p > pos for p, _, _ in calls) or pos < last_pos
                a["verify"] = "no_result" if later else "log_ends"
            else:
                a["outcome"] = outcome_of(hit[1])
                if hit[1] is not None and not str(hit[1]).strip():
                    a["verify"] = "empty_result"
        out.append(a)
    return out


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
    """Check every action. Calls of one case run in order (counts and outcomes depend on what came
    before); different cases run in parallel."""
    def one(a):
        rec = {"session": a["session"], "case_id": a["case_id"], "gold": a["gold"], "tool": a["tool"],
               "request": redact(a["request"])[:300]}
        for key in ("gold_call", "org", "verify", "outcome"):
            if a.get(key):
                rec[key] = a[key]
        action = a["action_text"] if a["action_text"] is not None else describe_action(a["tool"], a["input"])
        rec["action"] = redact(action)[:500]
        if a.get("context"):
            rec["tool_output"] = redact(a["context"])[:300]
        if a["tool"] in skip_tools:
            rec["decision"] = "skipped"
            return rec
        session = a.get("case_id") or a["session"]
        v = gate.check(a["request"], a["tool"], a["input"], action_text=a["action_text"], context=a.get("context"),
                       session=session, org=a.get("org"))
        if v.decision != "block" and a.get("outcome"):
            gate.settle(session, a["outcome"])
        rec.update({"decision": v.decision, "reasons": v.reasons, "scores": v.scores,
                    "latency_ms": round(v.latency_ms, 1), "cost": v.cost})
        if v.gap:
            rec["gap"] = v.gap
        if v.receipt:
            rec["receipt"] = v.receipt
        return rec

    groups, order = {}, []
    for i, a in enumerate(actions):
        key = a.get("case_id") or a["session"] or f"#{i}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((i, a))

    def run_group(key):
        return [(i, one(a)) for i, a in groups[key]]

    out = [None] * len(actions)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for chunk in pool.map(run_group, order):
            for i, rec in chunk:
                out[i] = rec
    return out


def repeat(make_gate, actions, times, workers=4, skip_tools=()):
    """Run the same actions several times with a fresh gate each time. Returns (first run, flips),
    where flips lists the calls whose decision was not the same in every run."""
    runs = [run(make_gate(), actions, workers, skip_tools) for _ in range(max(1, times))]
    flips = []
    for i, rec in enumerate(runs[0]):
        seen = [r[i].get("decision") for r in runs]
        if len(set(seen)) > 1:
            flips.append((rec.get("case_id") or rec["session"], i, seen))
    return runs[0], flips, len(runs)


def repeat_section(flips, times, n_calls):
    out = ["## Repeatability", "",
           f"{times} runs x {n_calls} calls: {len(flips)} call(s) changed decision between runs."]
    for key, i, seen in flips[:30]:
        out.append(f"- {key} (call {i + 1}): {' / '.join(seen)}")
    return out + [""]


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
    safe_rows = [row for row in scored if row[1] == "allow"]
    wrong_blocks = sum(1 for row in safe_rows if row[5])
    safe_asks = sum(1 for row in safe_rows if row[2] and not row[5])
    out = ["## Accuracy against your labels", "",
           f"False alarms: {wrong_blocks + safe_asks}/{c['safe']} safe {'records' if multi else 'cases'} "
           f"(wrongly blocked: {wrong_blocks}, sent to a person: {safe_asks})",
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


def per_call_section(records):
    """Score each call against its own label (gold on the call, or gold_calls on the record)."""
    rows = [r for r in records if r.get("gold_call") in ("allow", "block", "ask") and r.get("decision") in ("allow", "ask", "block")]
    if not rows:
        return []
    c = Counter()
    misses = []
    for r in rows:
        g, d = r["gold_call"], r["decision"]
        if g == "allow":
            c["safe"] += 1
            c["safe_ok"] += d == "allow"
            c["safe_blocked"] += d == "block"
            c["safe_asked"] += d == "ask"
        else:
            c["risky"] += 1
            c["risky_ok"] += d in ("ask", "block")
        if (g == "allow") != (d == "allow"):
            misses.append(r)
    out = ["## Per call", "",
           f"Correct: {c['safe_ok'] + c['risky_ok']}/{len(rows)} calls",
           f"- false alarms: {c['safe_blocked'] + c['safe_asked']}/{c['safe']} safe calls "
           f"(wrongly blocked: {c['safe_blocked']}, sent to a person: {c['safe_asked']})",
           f"- risky calls stopped: {c['risky_ok']}/{c['risky']}", ""]
    for r in misses[:40]:
        out.append(f"- {r.get('case_id') or r['session']}: gold {r['gold_call']}, got {r['decision']}"
                   f" ({', '.join(r.get('reasons', [])) or 'no reason'}) | {r['action'][:90]}")
    return out + ([""] if misses else [])


def verify_section(records, problems=()):
    """Calls the audit could only judge on their arguments, and why."""
    flagged = [r for r in records if r.get("verify")]
    if not flagged and not problems:
        return []
    reasons = Counter(r["verify"] for r in flagged)
    if problems:
        reasons["unreadable"] += len(problems)
    out = [f"## Couldn't verify ({sum(reasons.values())})", "",
           "These were not judged clean. The audit only saw part of what happened:"]
    for name, n in reasons.most_common():
        out.append(f"- {VERIFY_REASONS.get(name, name)}: {n}")
    for r in flagged[:15]:
        out.append(f"  - {r.get('case_id') or r['session']}: {r['verify']} | {r['action'][:90]}")
    return out + [""]


def coverage_line(records, problems=()):
    """What the audit saw, as numbers, so a clean report is never read as full coverage."""
    found = len(records)
    checked = sum(1 for r in records if r.get("decision") in ("allow", "ask", "block"))
    skipped = sum(1 for r in records if r.get("decision") == "skipped")
    failed = sum(1 for r in records if r.get("decision") == "error")
    partial = sum(1 for r in records if r.get("verify") and r.get("decision") in ("allow", "ask", "block"))
    parts = [f"checked {checked} of {found} tool calls found in these logs"]
    if partial:
        parts.append(f"{partial} judged on their arguments only (see Couldn't verify)")
    if skipped:
        parts.append(f"{skipped} skipped as read-only")
    if failed:
        parts.append(f"{failed} could not be checked")
    if problems:
        parts.append(f"{len(problems)} unreadable line(s)")
    return (f"**Coverage:** {'; '.join(parts)}. This audit only covers calls in these logs: "
            f"calls made outside them are not visible.")


def report(records, source, problems=(), top=15, flips=None, times=1):
    text = build(rows=records, top=top, title=f"# VexorGate trace audit: {source}")
    title, nl, body = text.partition("\n")
    text = title + nl + "\n" + coverage_line(records, problems) + "\n" + body
    extra = accuracy_section(records) + per_call_section(records) + verify_section(records, problems)
    if flips is not None and times > 1:
        extra += repeat_section(flips, times, len(records))
    if problems:
        extra += [f"## Lines that could not be read ({len(problems)})", ""] + [f"- {p}" for p in problems[:20]] + [""]
    if extra:
        head, sep, rest = text.partition("## ")
        text = head + "\n".join(extra) + "\n" + sep + rest if sep else text + "\n\n" + "\n".join(extra)
    return text
