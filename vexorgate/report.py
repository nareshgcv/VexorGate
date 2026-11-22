"""Shadow report: what the gate would have done over the last N days."""
import json
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import config


def load_records(days):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    logs = config.home() / "logs"
    records = []
    for f in sorted(logs.glob("*.jsonl")) if logs.exists() else []:
        if f.stem < cutoff:
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def build(days=7, top=15, rows=None, title=None):
    if rows is None:
        rows = load_records(days)
    checked = [r for r in rows if r.get("decision") in ("allow", "ask", "block", "error")]
    counts = Counter(r.get("decision") for r in rows)
    lat = sorted(r["latency_ms"] for r in checked if r.get("decision") != "error" and "latency_ms" in r)
    cost = sum(r.get("cost") or 0 for r in checked)
    reasons = Counter(x for r in checked if r["decision"] in ("block", "ask") for x in r.get("reasons", []))
    tools = Counter(r.get("tool") for r in checked if r["decision"] in ("block", "ask") and r.get("tool"))

    def pct(n):
        return f"{n} ({100 * n / len(checked):.0f}%)" if checked else "0"

    interrupts = counts["ask"] + counts["block"]
    head = []
    if rows is None or title is None:
        per_week = interrupts * 7 / max(1, days)
        head = [f"**Interruptions: about {per_week:.0f} per week** ({counts['block']} blocked, {counts['ask']} asked you, "
                f"over {days} days). Each one is a possible false alarm: if most of them were fine, "
                f"loosen the policy before you turn on enforce mode.", ""]
    out = [title or f"# VexorGate shadow report (last {days} days)", ""] + head + [
           f"Tool calls checked: {len(checked)} (skipped read-only: {counts['skipped']})", "",
           "| Would have | Calls |", "|---|---|",
           f"| allowed | {pct(counts['allow'])} |",
           f"| asked you | {pct(counts['ask'])} |",
           f"| blocked | {pct(counts['block'])} |",
           f"| failed to check | {pct(counts['error'])} |", ""]
    if lat:
        p90 = lat[min(len(lat) - 1, int(0.9 * len(lat)))]
        out.append(f"Decision time: median {statistics.median(lat):.0f} ms, p90 {p90:.0f} ms. Total cost: ${cost:.6f}.")
        out.append("")
    if reasons:
        out += ["## Most common reasons", ""] + [f"- {name}: {n}" for name, n in reasons.most_common()] + [""]
    if tools:
        per_tool = {}
        for r in checked:
            per_tool.setdefault(r.get("tool") or "(text)", Counter())[r["decision"]] += 1
        out += ["## Per tool", "", "| Tool | Calls | Allowed | Asked | Blocked |", "|---|---|---|---|---|"]
        for name, _ in tools.most_common():
            c = per_tool.get(name, Counter())
            out.append(f"| {name} | {sum(c.values())} | {c['allow']} | {c['ask']} | {c['block']} |")
        out.append("")
    flagged = [r for r in checked if r["decision"] in ("block", "ask")]
    out += [f"## Would have stopped ({len(flagged)})", ""]
    for r in flagged[-top:]:
        where = f" on `{r['tool']}`" if r.get("tool") else ""
        label = f"{r['case_id']}: " if r.get("case_id") else ""
        out.append(f"- {label}**{r['decision']}** ({', '.join(r.get('reasons', []))}){where}")
        out.append(f"  - asked: {r.get('request', '')[:150]}")
        out.append(f"  - action: {r.get('action', '')[:200]}")
        if r.get("gap"):
            out.append(f"  - gap: {r['gap']}")
        if r.get("receipt"):
            out.append(f"  - exact action: {r['receipt']}")
    if not flagged:
        out.append("- nothing")
    errors = [r for r in checked if r["decision"] == "error"]
    if errors:
        out += ["", f"## Check failures ({len(errors)})", "", f"- latest: {errors[-1].get('reasons', [''])[0]}"]
    return "\n".join(out)
