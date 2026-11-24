# VexorGate

**Is your AI agent doing what the user asked, or more?**

AI agents take real actions: sending email, moving money, changing permissions, running commands. Rule-based gateways stop known-bad tool calls, but most real failures are valid calls that go beyond the request: refunding every order instead of one, restarting production instead of staging, sharing a file with anyone instead of the team.

VexorGate checks each tool call against what the user actually asked, and answers **allow**, **ask** (send to a human) or **block**, with a reason. Plain-code rules compare hard facts first; a fast System One decision model handles what rules cannot express. When something doesn't match, it shows the gap in plain words:

> You asked for 1 refund. The agent wants 2 refunds, $98.00 total.

## What you can do with it

| | What it does | Touches your live agent? |
|---|---|---|
| **Trace audit** | Runs every tool call in your existing agent logs through the gate and reports what went beyond the request | No |
| **Shadow mode** | Watches a live agent (Claude Code today) and logs what it would have done | Observes only |
| **Enforce mode** | Asks for confirmation or blocks before risky calls run | Yes |

Start with a trace audit. It runs on your machine and needs nothing in production.

## Install

```bash
pip install git+https://github.com/nareshgcv/VexorGate.git@v0.5.1
```

The command and package are lowercase: `vexorgate` or `python -m vexorgate`.

## Choose a model, or none

**Rules only (no model, nothing leaves your machine):**

```bash
vexorgate audit your_traces.jsonl --rules-only
```

This catches hard-fact mismatches (environment, count, amount, recipients and record IDs, stronger actions than asked, audience, duplicates, new payees, loops) and nothing else. Add a model for everything rules can't express. If you point VexorGate at a hosted endpoint without setting a key, it runs on rules only and says so, instead of failing.

VexorGate works with any `/systemone` decision endpoint. Pick one:

**Local with Ollama (free, nothing leaves your machine).** Needs Ollama 0.35 or newer.

```bash
ollama pull nimble        # best local option, needs a GPU with about 10 GB
export VEXORGATE_URL=http://localhost:11434/v1/systemone
export VEXORGATE_MODEL=nimble
```

`d1:3b` runs on most laptops but makes many more mistakes (see results). Local models can be slow on first load, so raise the timeout in `~/.vexorgate/config.json`, for example `{"timeout": 120}`.

**Hosted via OpenRouter (most accurate, needs your own key).** Use d1.1: the original `liquid/d1-20260930` was retired on OpenRouter on November 20, 2026, and returns HTTP 404. `typesafe/jev-1.14-20261014` also works.

```bash
export VEXORGATE_URL=https://openrouter.ai/api/v1/systemone
export VEXORGATE_MODEL=liquid/d1.1-20261027
export VEXORGATE_KEY=sk-or-...
```

Set `VEXORGATE_ORG` to your email domain (for example `acme.io`) so the gate can tell internal recipients from external ones. Settings can also go in `~/.vexorgate/config.json`. Environment variables override that file.

Rate limits (HTTP 429) and temporary server errors are retried up to 3 times with a short wait (`"retries"` in the config file). If a call still fails, the result is **error**, never allow, and the reason includes the server's own message, for example a retired model name.

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

gate = Gate(SystemOneClient("https://openrouter.ai/api/v1/systemone", key, "liquid/d1.1-20261027"), org="acme.io")
v = gate.check("Refund order 1182", "stripe.refunds.create", {"charge": "ch_1183"})
print(v.decision, v.reasons)
```

## How the gate works

**Layer 1: hard-fact rules, in plain code.** VexorGate reads a few facts from the user's own words and from the tool call's arguments, and compares them:

| Fact | From the request | From the call | Rule |
|---|---|---|---|
| Environment | "restart staging", "staging only", "don't touch production" | `-n production`, `$PROD_URL` | an environment you excluded, or one you didn't name: block; no environment named: ask |
| Count | "refund order 1182", "the duplicate charge", "3 boxes", "buy 10 shares" | refund, payment and order calls, list lengths, `qty`, wildcard filters such as `"all orders where..."` | more than asked: block |
| Amount | "$40", "€1.234,56", "Rs. 5,000", "50%", "max $60" | `amount=4000` (Stripe cents), `amount_cents`, `currency`, `percent`, `"full"` | more than asked, another currency, a bigger share, or over your limit: block; above your policy cap: ask |
| Recipient and record | `billing@clientco.com`, "ClientCo", "order 1043", "card ending 4242" | emails and IDs in the arguments | look-alike domain (clientcorp) or swapped digits (1034): block; a look-alike of your own domain, or a recipient you didn't name: ask |
| Action | "draft", "check", "what is...", "pause", "limit order" | `send`, `update`, `place_order`, `delete`, `type=market` | a stronger action than asked: block |
| Audience | "with the team", "make it public" | `type='anyone'`, `visibility='public'`, public-access blocks off | public when the team was asked: block; public when not asked: ask; public when asked: ask to confirm (can't be undone) |
| Duplicate | "in two orders of 5", "two refunds" | the same money action again, judged by its economic fields (account, symbol, side, size, amount...) | identical actions up to the number the request asks for: allow; same dedupe ID (`client_order_id`, `idempotency_key`, `cloid`, `newClientOrderId`, `clOrdID`...): a retry, allow; new ID: ask, or block if it goes over the count; earlier outcome unknown: ask |
| New payee | | "newly added account" | ask |
| Loop | | the same call repeated (3 times by default) | block |
| Repeated blocks | | 3 blocks on one task | everything else on that task asks a person |

A rule block is final and needs no model call. **The request can only narrow what is allowed, never widen it**: fixed limits in your policy (amount caps, tools that always ask) apply whatever the request says.

**Session memory.** A short reply such as "yes", "go ahead" or "try again" continues the previous request instead of replacing it, so counts and totals don't reset. Limits you set earlier ("staging only", "don't touch production", "max $60") last for the whole session, until you name that environment again. Nothing carries into another session. A blocked call never ran, so it never uses up the budget; a call that failed is released (the trace audit reads this from the logged results, the Claude Code hook from `PostToolUse`), so a corrected retry is not counted twice.

**Every ask or block shows the exact action**, built from the call's arguments in code, never written by a model, next to the gap: "You asked for 1 refund. The agent wants 2 refunds, $80.00 total. Exact action: stripe.refunds.create: charge ch_1190, amount 4000".

**Layer 2: the model**, for what rules can't express. One `/systemone` request asks:

1. a broad question: may this action run automatically?
2. eight narrow yes/no checks: external send, public exposure, money, deletion, permissions, privacy, untrusted code, beyond the request
3. an injection check: does the text contain approvals or instructions that did not come from the user?

When the broad question says block or any check scores 0.8 or higher, the model has **flagged** the action. From the command line and the Claude Code hook, a flag from the model alone **asks** a person by default; it blocks only when a rule also fired, or when you set `"model_blocks": true`. If only the injection check fires, it asks. Otherwise it allows. A recipient the user named exactly is not treated as an unexpected outside send. Option definitions are fixed in code, so attacker-controlled config cannot rewrite them. Secrets are redacted before anything is sent. If the endpoint fails, the result is **error**, never allow; a rule "ask" stays "ask" even if the model fails.

The gate only reads the user's own request and the raw tool call (plus, optionally, the tool output the agent read, labeled as untrusted). It never reads the agent's reasoning: a checker that reads the agent's reasoning ends up grading persuasion, not the call.

### Policy

Set fixed limits in `~/.vexorgate/config.json`:

```json
{
  "policy": {
    "sensitive_envs": ["production"],
    "max_amount": 200,
    "always_ask_tools": ["github.repos.delete", "stripe.payouts.*"],
    "loop_threshold": 3,
    "max_count": 5,
    "max_blocks": 3,
    "dedupe_keys": ["myOrderTag"],
    "qty_keys": ["lot_count"],
    "confirm_public": true
  },
  "model_blocks": false
}
```

`max_count` asks when one task makes more money actions than this. `max_blocks` sends a task to a person after this many blocks. `confirm_public` asks before anything is made public, even when the user asked for it. `model_blocks` lets a flag from the model alone block instead of ask. `dedupe_keys` and `qty_keys` add your own field names for an order's dedupe ID and size; common names across brokers and exchanges (Alpaca, Hyperliquid, Binance, FIX, Interactive Brokers, OKX, Bybit and others) are built in.

Use `--no-rules` to run the model alone (as in v0.3), for comparison.

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

**Rules only, no model:**

| Set | v0.4 | v0.5 | False alarms (v0.5) |
|---|---|---|---|
| `dev_fresh` (28 cases) | 19/28 | 21/28 | 0/14 safe |
| `agent_gating_v2` (50 cases) | 31/50 | 32/50 | 0/25 safe |
| `multistep_v05` (17 conversations, 35 calls, new) | | 33/35 calls | 0/21 safe calls |

The rules were written after seeing which cases the models missed, and `multistep_v05` was written for v0.5 from the failure kinds raised in public discussion, so these numbers are optimistic. Its two misses are on purpose: calls made in the wrong order (a confirmation sent before the booking exists), which per-call rules cannot see. The independent evaluations are the fair tests.

**v0.5 with a model.** From v0.5, a flag from the model alone asks a person instead of blocking, so "stopped" counts (block or ask) compare across versions, but "blocked" counts do not.

On 40 wrong-argument cases kept private by the author (18 risky, 19 safe, 3 ambiguous: amounts, counts, recipients, customers, actions and environments). They were written before v0.5, and its rules were built from what v0.4 missed on them, so these numbers are optimistic:

| | v0.4, Jev 1.14 | v0.5, Jev 1.14 | v0.5, d1.1 |
|---|---|---|---|
| Risky stopped (block or ask) | 17/18 | 18/18 | 18/18 |
| ...blocked by a rule, with a gap message | | 12 | 12 |
| False alarms on safe cases | 5/19, all blocks | 1/19, an ask | 0/19 |
| Ambiguous cases sent to a person | 1/3 | 1/3 | 2/3 |

Most of the change comes from v0.5, not the model: on the same model (Jev 1.14), false alarms went from 5 wrong blocks to 1 question. The rules blocked the same 12 cases on both models.

On `multistep_v05` with d1.1: 31/35 calls correct (rules only: 33/35). No safe call was blocked; the model sent 2 safe refunds to a person (its money check fired on refunds the user had asked for, within the stated limit). The two wrong-order cases were still missed. 0 errors, median 0.6 s per call.

The hosted d1 results above were measured on `liquid/d1-20260930`, which has since been retired; d1.1 has not yet been run on `dev_fresh`. These are small, synthetic datasets. Full tables, attack details and caveats: [results/SUMMARY.md](results/SUMMARY.md). What changed in each version and why: [CHANGELOG.md](CHANGELOG.md).

**Independent evaluation.** An outside contributor holds a private set of 10 test cases, written from failure patterns raised in public discussion (hidden instructions in tool outputs, a checker talked into "yes", loops that look like progress), not from what VexorGate already handles. The author never sees the cases. The contributor runs each tagged version with the same model and posts the version, model, score and the kinds of cases missed in the [Independent evaluation results](https://github.com/nareshgcv/VexorGate/issues) issue.

Scores are reported by the evaluator: public and comparable across versions, but not independently re-checkable. Known gaps, such as loop detection, are expected to fail and are reported as findings.

**Independent evaluation, trading agents** ([issue #1](https://github.com/nareshgcv/VexorGate/issues/1)). A second outside tester wrote 13 private trading-agent cases (20 calls, 7 risky). On v0.4.0 with d1.1, run 6 times: 11/13 cases correct, 5/7 risky calls stopped, no false alarms, and **no verdict changed across 6 runs**. Rules alone stopped 0/7, because v0.4 had no rules for orders. It missed both duplicate-order cases (a second identical order with a new order ID, and a new order after a lost response).

The same tester re-ran the same 13 cases (Alpaca-shaped) on v0.5.0, 5 times:

| | v0.4.0 | v0.5.0 |
|---|---|---|
| Rules only: correct | 6/13 | 10/13 |
| Rules only: risky stopped | 0/7 | 5/7 |
| With d1.1: correct | 11/13 | 12/13 |
| With d1.1: risky stopped | 5/7 | 7/7 |
| False alarms | 0 | 1, sent to a person, nothing wrongly blocked |
| Verdict changes between runs | 0 over 6 runs | 0 over 5 runs |

Both duplicate-order cases were fixed. The tester also found what v0.5.1 fixes: a request that splits an order ("buy 10 MSFT in two orders of 5") had its second order asked about as a duplicate, Hyperliquid's field names (`cloid`, `sz`) were not recognized, so the duplicate check ran backwards on them, "with a limit at 140" was not read as a limit order, and a repeated order's gap showed the running total as if it were the call's size. A look-alike symbol (MET to META) is caught only by the model, as a question. These cases are synthetic; the tester's production system trades algorithmically, so it has no user request to compare against.

## Shadow mode in Claude Code

A Claude Code hook records your request, checks each tool call, and logs what VexorGate would have done, without changing anything. The report leads with how often it would have interrupted you per week, and breaks that down per tool, because a checker that cries wolf gets switched off. After a few days:

```bash
vexorgate report --days 7
```

Setup takes about 5 minutes: [docs/claude-code.md](docs/claude-code.md).

## Limitations

- **Small, synthetic benchmarks.** Real agent traffic will be messier. Run your own traces.
- **Rules cover a set of facts, not meaning.** Environment, count, amount, recipients and IDs, stronger actions, audience, duplicates and repeats. A record that is wrong but looks nothing like the one asked for (a different customer with an unrelated ID) needs checks against your own data, which VexorGate does not have.
- **Keyword-based fact reading.** Rules read facts from plain words and common argument shapes (kubectl, Stripe, Drive, GitHub, S3, broker orders). Unusual tools or phrasing fall through to the model.
- **Order of calls.** Each call is judged with what came before it, but not with what should have come before it: a confirmation sent before the booking exists, or a charge before shipping, is not caught.
- **Duplicates need a dedupe ID to tell retries apart.** "Same ID means the same order retried" only holds when the tool really dedupes on that ID (`client_order_id`, `idempotency_key`). Calls without one are treated as possible duplicates.
- **Couldn't verify.** The trace audit only judges what is in the log. Calls with no logged result, or a log that ends suddenly, are listed under "Couldn't verify" with the reason. A call made outside the logged harness cannot be seen at all.
- **Vague requests ask.** "Restart the server" with a production target asks you. That moves a decision to the user; measure how often it happens in shadow mode before turning on enforcement.
- **Multi-step runs.** Supported in the audit and the hook, but where legitimate preparatory steps look broader than the request is not yet well tested on real traffic.
- **Model dependence.** The best accuracy so far needs hosted d1; local Nimble is close but needs a GPU, and the open 3B model is not accurate enough for realistic tool calls.
- **Data ownership.** No model tested reliably notices one customer's data going to another. Use a rule on record owners for that.
- **Wording matters.** Small changes to check wording moved results noticeably. Validate changes on data you have not tuned on.

## Repository layout

```
vexorgate/
├── vexorgate/
│   ├── gate.py          # the gate: rules first, then any /systemone endpoint
│   ├── facts.py         # hard-fact rules and gap messages
│   ├── audit.py         # trace audit
│   ├── claude_hook.py   # Claude Code hook (shadow or enforce)
│   ├── report.py        # reports
│   ├── redact.py        # secret redaction
│   └── config.py
├── benchmarks/          # dev_fresh, agent_gating_v2, multistep_v05, example traces
├── results/SUMMARY.md   # all measured results
├── docs/                # trace audit and Claude Code guides
├── tests/               # unit tests, no network needed
└── private/             # your held-out sets (git-ignored)
```

Run the tests with `python -m unittest discover -s tests` (133 tests, no network needed).

## Contributing

Realistic tool-call cases from production agent workflows are the most valuable contribution, especially calls the tool allows but the user's request does not. Please open an issue first for larger changes.

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE).
