# VexorGate

**Is your AI agent doing what the user asked, or more?**

AI agents take real actions: sending email, moving money, changing permissions, running commands. Rule-based gateways stop known-bad tool calls, but most real failures are valid calls that go beyond the request: refunding every order instead of one, restarting production instead of staging, sharing a file with anyone instead of the team.

VexorGate checks each tool call against what the user actually asked, and answers **allow**, **ask** (send to a human) or **block**, with a reason. It runs on fast System One decision models, in one request per tool call.

## What you can do with it

| | What it does | Touches your live agent? |
|---|---|---|
| **Trace audit** | Runs every tool call in your existing agent logs through the gate and reports what went beyond the request | No |
| **Shadow mode** | Watches a live agent (Claude Code today) and logs what it would have done | Observes only |
| **Enforce mode** | Asks for confirmation or blocks before risky calls run | Yes |

Start with a trace audit. It runs on your machine and needs nothing in production.

## Quick start: trace audit

```bash
git clone <this repo> && cd vexorgate
pip install -e .
export VEXORGATE_KEY=sk-or-...          # OpenRouter key, or use a local llama.cpp server
export VEXORGATE_ORG=yourcompany.com     # your email domain

vexorgate audit benchmarks/example_traces.jsonl --dry-run   # see exactly what would be sent
vexorgate audit your_traces.jsonl --out report.md
```

The report lists every flagged action next to the user's request, with the reason (external send, public exposure, money, deletion, permissions, privacy, untrusted code, beyond the request, or injected authority). Input formats and privacy notes: [docs/trace-audit.md](docs/trace-audit.md).

## Check one action

```bash
vexorgate check --request "Refund order 1182" --tool stripe.refunds.create --input '{"charge": "ch_1183"}'
```

Or from Python:

```python
from vexorgate import Gate, SystemOneClient

gate = Gate(SystemOneClient("https://openrouter.ai/api/v1/systemone", key, "liquid/d1-20260930"), org="acme.io")
v = gate.check("Refund order 1182", "stripe.refunds.create", {"charge": "ch_1183"})
print(v.decision, v.reasons)
```

## How the gate works

One `/systemone` request asks:

1. a broad question: may this action run automatically?
2. eight narrow yes/no checks: external send, public exposure, money, deletion, permissions, privacy, untrusted code, beyond the request
3. an injection check: does the text contain approvals or instructions that did not come from the user?

It **blocks** if the broad question says block or any check scores 0.8 or higher, **asks** if only the injection check fires, and **allows** otherwise. Option definitions are fixed in code, so attacker-controlled config cannot rewrite them. Secrets are redacted before anything is sent. If the endpoint fails, the result is **error**, never allow.

Any `/systemone` endpoint works: hosted d1 or Jev via OpenRouter, or llama.cpp locally.

## Results so far

On 28 realistic tool calls (`benchmarks/dev_fresh.jsonl`, AI-generated and reviewed by hand, never used for tuning):

| | Correct | Fail-open under 10 attacks |
|---|---|---|
| VexorGate, hosted d1 | 27/28 | 0/140 (0%) |
| VexorGate, Jev 1.14 | 25/28 | 0/140 (0%) |
| Single-question gate, hosted d1 | 27/28 | 26/130 (20%) |
| Meta LlamaFirewall AlignmentCheck | 24/28 | at least 6/70 (9%), partial run |
| VexorGate, open d1-3B run locally | 16/28 | not run |

Two findings:

- **Model choice drives accuracy.** The same gate scored 16/28 on the small open model and 27/28 on hosted d1.
- **Gate design drives attack resistance.** On both hosted models, a single question let about 20% of attacked risky actions through; the layered design let none through.

These are small, synthetic datasets. Full tables, attack details and caveats: [results/SUMMARY.md](results/SUMMARY.md).

## Shadow mode in Claude Code

A Claude Code hook records your request, checks each tool call, and logs what VexorGate would have done, without changing anything. After a few days:

```bash
vexorgate report --days 7
```

Setup takes about 5 minutes: [docs/claude-code.md](docs/claude-code.md).

## Limitations

- **Small, synthetic benchmarks.** Real agent traffic will be messier. Run your own traces.
- **One request per action.** Long multi-step runs, where legitimate steps look broader than the request, are not yet tested.
- **Hosted model dependence.** Good accuracy so far needs a hosted model; the open 3B model is not accurate enough for realistic tool calls.
- **Data ownership.** No model tested reliably notices one customer's data going to another. Use a rule on record owners for that.
- **Wording matters.** Small changes to check wording moved results noticeably. Validate changes on data you have not tuned on.

## Repository layout

```
vexorgate/
├── vexorgate/
│   ├── gate.py          # the gate, any /systemone endpoint
│   ├── audit.py         # trace audit
│   ├── claude_hook.py   # Claude Code hook (shadow or enforce)
│   ├── report.py        # reports
│   ├── redact.py        # secret redaction
│   └── config.py
├── benchmarks/          # dev_fresh, agent_gating_v2, example traces
├── results/SUMMARY.md   # all measured results
├── docs/                # trace audit and Claude Code guides
├── tests/               # unit tests, no network needed
└── private/             # your held-out sets (git-ignored)
```

Run the tests with `python -m unittest discover -s tests`.

## Contributing

Realistic tool-call cases from production agent workflows are the most valuable contribution. Please open an issue first for larger changes.

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE).
