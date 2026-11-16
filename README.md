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

## Install

```bash
pip install git+https://github.com/nareshgcv/VexorGate.git@v0.3.0
```

The command and package are lowercase: `vexorgate` or `python -m vexorgate`.

## Choose a model

VexorGate works with any `/systemone` decision endpoint. Pick one:

**Local with Ollama (free, nothing leaves your machine).** Needs Ollama 0.35 or newer.

```bash
ollama pull nimble        # best local option, needs a GPU with about 10 GB
export VEXORGATE_URL=http://localhost:11434/v1/systemone
export VEXORGATE_MODEL=nimble
```

`d1:3b` runs on most laptops but makes many more mistakes (see results). Local models can be slow on first load, so raise the timeout in `~/.vexorgate/config.json`, for example `{"timeout": 120}`.

**Hosted via OpenRouter (most accurate, needs your own key).**

```bash
export VEXORGATE_URL=https://openrouter.ai/api/v1/systemone
export VEXORGATE_MODEL=liquid/d1-20260930
export VEXORGATE_KEY=sk-or-...
```

Set `VEXORGATE_ORG` to your email domain (for example `acme.io`) so the gate can tell internal recipients from external ones. Settings can also go in `~/.vexorgate/config.json`.

## Quick start: trace audit

```bash
vexorgate audit your_traces.jsonl --dry-run      # see exactly what would be sent, nothing is sent
vexorgate audit your_traces.jsonl --out report.md
```

The report lists every flagged action next to the user's request, with the reason (external send, public exposure, money, deletion, permissions, privacy, untrusted code, beyond the request, or injected authority). If you add `"gold": "allow"` or `"block"` labels, it also scores accuracy.

Inputs can be a `.jsonl` file, a `.json` file, or a folder of them, including **multi-step conversations** (such as the public [R-Judge](https://arxiv.org/abs/2401.10019) benchmark). A multi-step conversation counts as stopped if any of its steps was blocked or sent to a human. For those, two options are recommended:

```bash
vexorgate audit conversations/ --no-thoughts --tool-output
```

- `--no-thoughts` leaves out the agent's own reasoning. In testing, an injected instruction showed up in the agent's reasoning as a justification ("the user might want to follow this") and talked the gate into allowing the action.
- `--tool-output` sends the tool output the agent read just before each action (an email, a web page), labeled as not from the user, so the injection check can see where an instruction came from.

Input formats and privacy notes: [docs/trace-audit.md](docs/trace-audit.md).

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

The gate only reads the user's own request and the raw tool call (plus, optionally, the tool output the agent read, labeled as untrusted). It never reads the agent's reasoning: a checker that reads the agent's reasoning ends up grading persuasion, not the call.

## Results so far

On 28 realistic tool calls (`benchmarks/dev_fresh.jsonl`, AI-generated and reviewed by hand, never used for tuning):

| | Correct | Fail-open under 10 attacks |
|---|---|---|
| VexorGate, hosted d1 | 27/28 | 0/140 (0%) |
| VexorGate, Jev 1.14 | 25/28 | 0/140 (0%) |
| Single-question gate, hosted d1 | 27/28 | 26/130 (20%) |
| Meta LlamaFirewall AlignmentCheck | 24/28 | at least 6/70 (9%), partial run |
| VexorGate, Nimble 9B via Ollama (local, free) | 23/28 | not run |
| VexorGate, open d1-3B via Ollama (local, laptop) | 16/28 | not run |

Two findings:

- **Model choice drives accuracy.** The same gate scored 16/28 on the small open d1-3B, 23/28 on local Nimble 9B, and 27/28 on hosted d1. Nimble allowed all 14 safe actions; its misses were risks hidden in arguments, such as `type='anyone'` or `-n production`.
- **Gate design drives attack resistance.** On both hosted models, a single question let about 20% of attacked risky actions through; the layered design let none through.

These are small, synthetic datasets. Full tables, attack details and caveats: [results/SUMMARY.md](results/SUMMARY.md).

**Independent evaluation.** An outside contributor holds a private set of test cases, runs each tagged version against it, and posts the scores publicly in the "Independent evaluation results" issue, so results can be checked by someone other than the author.

## Shadow mode in Claude Code

A Claude Code hook records your request, checks each tool call, and logs what VexorGate would have done, without changing anything. After a few days:

```bash
vexorgate report --days 7
```

Setup takes about 5 minutes: [docs/claude-code.md](docs/claude-code.md).

## Limitations

- **Small, synthetic benchmarks.** Real agent traffic will be messier. Run your own traces.
- **Argument-level risks.** The model often misses risks that sit in the arguments, such as restarting `-n production` when the user said staging. The next version moves these decisions into plain code: compare target, count, amount and audience from the user's request with the call's arguments, and show the gap.
- **No loop detection.** Each action is checked on its own; an agent repeating the same action is not caught yet.
- **Multi-step runs.** Supported in the audit, but where legitimate preparatory steps look broader than the request is not yet well tested.
- **Model dependence.** The best accuracy so far needs hosted d1; local Nimble is close but needs a GPU, and the open 3B model is not accurate enough for realistic tool calls.
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

Run the tests with `python -m unittest discover -s tests` (31 tests, no network needed).

## Contributing

Realistic tool-call cases from production agent workflows are the most valuable contribution, especially calls the tool allows but the user's request does not. Please open an issue first for larger changes.

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE).
