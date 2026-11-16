"""Command line:
  vexorgate check  ...           try the gate on one action
  vexorgate audit  traces.jsonl  check every tool call in existing agent logs
  vexorgate report               summarize the shadow-mode logs
"""
import argparse
import json
import os
import sys

from . import audit, config
from .gate import Gate, SystemOneClient
from .report import build


def make_gate(cfg):
    return Gate(SystemOneClient(cfg["url"], cfg["key"], cfg["model"], int(cfg["timeout"])), cfg.get("org", ""))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vexorgate")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="check one action")
    c.add_argument("--request", required=True, help="what the user asked")
    c.add_argument("--tool", required=True, help="tool name, e.g. Bash")
    c.add_argument("--input", default="{}", help="tool input as JSON, or plain text")

    a = sub.add_parser("audit", help="check every tool call in a .jsonl trace file")
    a.add_argument("file", help="a .jsonl or .json file, or a folder of them (e.g. the R-Judge data folder)")
    a.add_argument("--out", help="write the Markdown report here (default: print it)")
    a.add_argument("--results", help="also write one JSON result per tool call here")
    a.add_argument("--workers", type=int, default=4, help="parallel requests (default 4)")
    a.add_argument("--limit", type=int, help="only check the first N cases (whole multi-step records)")
    a.add_argument("--dry-run", action="store_true",
                   help="parse and show what would be sent, without calling the endpoint")
    a.add_argument("--top", type=int, default=15)
    a.add_argument("--no-thoughts", action="store_true",
                   help="multi-step records: judge the agent's actions only, not its own reasoning")
    a.add_argument("--tool-output", action="store_true",
                   help="multi-step records: also send the tool output the agent read before each action")

    r = sub.add_parser("report", help="summarize the shadow logs")
    r.add_argument("--days", type=int, default=7)
    r.add_argument("--top", type=int, default=15)
    args = ap.parse_args(argv)

    if args.cmd == "report":
        print(build(args.days, args.top))
        return 0

    cfg = config.load()

    if args.cmd == "check":
        try:
            tool_input = json.loads(args.input)
        except json.JSONDecodeError:
            tool_input = args.input
        v = make_gate(cfg).check(args.request, args.tool, tool_input)
        print(json.dumps({"decision": v.decision, "reasons": v.reasons, "scores": v.scores,
                          "latency_ms": round(v.latency_ms, 1), "cost": v.cost}, indent=2))
        return 0

    actions, problems = audit.load_actions(args.file, thoughts=not args.no_thoughts, tool_output=args.tool_output)
    if args.limit:
        keep, seen = [], set()
        for i, act in enumerate(actions):
            key = act.get("case_id") or f"#{i}"
            if key not in seen:
                if len(seen) >= args.limit:
                    break
                seen.add(key)
            keep.append(act)
        actions = keep
    n_cases = len({act.get("case_id") or f"#{i}" for i, act in enumerate(actions)})
    print(f"{len(actions)} tool calls in {n_cases} cases found in {args.file}" + (f", {len(problems)} unreadable lines" if problems else ""),
          file=sys.stderr)

    if args.dry_run:
        gate = make_gate(cfg)
        for a_ in actions[:5]:
            print(gate.state(a_["request"], a_["tool"], a_["input"], a_["action_text"], a_.get("context")))
        if len(actions) > 5:
            print(f"... and {len(actions) - 5} more", file=sys.stderr)
        for p in problems[:10]:
            print("unreadable:", p, file=sys.stderr)
        return 0

    if not cfg["key"] and "openrouter.ai" in cfg["url"]:
        print("No API key set. Add it to ~/.vexorgate/config.json or set VEXORGATE_KEY.", file=sys.stderr)
        return 2

    records = audit.run(make_gate(cfg), actions, args.workers, cfg.get("skip_tools", []))
    text = audit.report(records, os.path.basename(args.file), problems, args.top)
    if args.results:
        with open(args.results, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"Report written to {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
