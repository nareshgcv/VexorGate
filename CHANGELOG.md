# Changelog

Each change notes what prompted it, so results can be read next to the reason for the change.

## v0.5.2

**Security fixes for the Claude Code hook.** No change to what the gate decides: rules and model are the same as v0.5.1, and all earlier benchmark results stand.

- **The hook fails closed in enforce mode.** Before, any error inside the hook was logged and the hook printed nothing, and Claude Code runs a tool when its hook gives no answer. A half-written session file (possible when two hooks wrote at once), a broken `config.json` or an unexpected input therefore let the action through, even one that should have been blocked. Now any such problem answers "ask" with the reason. Enforce mode is still recognized when the config file itself is broken (from `VEXORGATE_MODE`, or the file's text). A damaged session file is set aside, and the next call asks once because earlier counts were lost. Shadow mode still only logs.
- **A time limit for each check.** Retries on rate limits could wait up to 30 seconds each, past Claude Code's hook timeout. Now the whole check, retries and waits included, stops at `deadline` (15 seconds by default) and answers error, which the hook turns into ask.
- **Private, redacted files.** Session files stored raw prompts and full tool arguments, readable by other users on the machine. Now prompts are redacted before they are saved, the ledger keeps redacted call fields, and `~/.vexorgate`, `sessions/` and `logs/` are owner-only (0700, files 0600) on macOS and Linux.
- **Parallel tool calls.** Session files are now written atomically and behind a per-session lock, so hooks running at the same time no longer lose each other's counts or leave a damaged file. Tested with 10 hook processes at once: 10 of 10 calls recorded.

Found in a security review of v0.5.1, with the fail-open case reproduced: a production restart after a request for staging got no answer from the hook in enforce mode when its session file was damaged.

Not changed yet (on the v0.6 list): refusing a plain `http://` endpoint for remote hosts and not following redirects with the API key, warning on config typos such as `"mode": "enforced"`, showing tool arguments as code in audit reports, and naming session files by a hash.

## v0.5.1

**Fixes from the trading tester's v0.5.0 run, and a coverage line in every audit report.** No change on the existing benchmarks (rules only: `dev_fresh` 21/28, `agent_gating_v2` 32/50, `multistep_v05` 33/35 calls, no false alarms).

- **Field names across venues.** The duplicate check now recognizes dedupe IDs and sizes by their names on common brokers and exchanges: `cloid` and `sz` (Hyperliquid), `newClientOrderId` and `origQty` (Binance), `clOrdID` and `orderQty` (FIX), `orderRef` and `totalQuantity` (Interactive Brokers), `clientOid` (OKX, KuCoin), `orderLinkId` (Bybit), and more. Names match regardless of case, `_` or `-`. On Hyperliquid, v0.5.0 had it backwards: a new `cloid` after a timeout passed, and a same-`cloid` retry was sent to a person. Orders are also recognized from `is_buy`, or from a size on a tool named "order".
- **Your own field names.** New `dedupe_keys` and `qty_keys` policy settings.
- **Identical orders the request asks for are not duplicates.** The number comes from the request: "buy 10 MSFT in two orders of 5" allows two identical orders, "in 3 tranches" three, "two refunds" two. With no split stated, the second identical order is still a duplicate. In v0.5.0 the second tranche was asked about, a regression from v0.4.
- **Clearer gap for running totals.** When earlier calls in the task add up, the gap keeps the call apart from the total: "You asked for 5 shares. This call is 5 shares, which would make 10 shares in total in this task." v0.5.0 said "the agent wants 10 shares" for a 5-share call.
- **More ways to say "limit".** "with a limit at 140", "limit at 140", "using a limit" are now read as a limit order. A request that says "at market" or "market order" is not.
- **Coverage line** at the top of every audit report, in numbers: tool calls checked out of those found, how many were judged on their arguments only, skipped or unreadable, and that calls outside the logs are not visible.

Prompted by:
- The trading tester's v0.5.0 rerun (issue #1): 13 Alpaca-shaped cases, 5 runs; rules only 10/13, with d1.1 12/13 and 7/7 risky stopped, 0 verdict changes. The tester reported the split-order regression, the Hyperliquid field names, the limit phrasing and the running-total wording, and asked that the number of allowed identical orders come from the request rather than a fixed number.
- Public discussion: a clean audit report should say what it covers in numbers, because the gap itself is usually the finding.

## v0.5.0

**More hard-fact rules, session memory, and the model asks instead of blocking on its own.** Rules-only results: `dev_fresh` 21/28 (v0.4: 19/28), `agent_gating_v2` 32/50 (v0.4: 31/50), new `multistep_v05` 33/35 calls, with no false alarms on any safe case. Not yet run with a model.

- **Recipient and record rule.** Emails, domains and IDs named in the request are compared with the arguments: a look-alike domain (clientco / clientcorp), another ending of a named domain, or swapped digits (1043 / 1034, card 4242 / 4224) block; a look-alike of your own domain, a recipient you didn't name, or a similar customer name ask. A recipient the user named exactly no longer trips the model's external-send check.
- **The model alone asks.** From the command line and the Claude Code hook, a flag from the model alone now sends the action to a person instead of blocking it. A rule plus a model flag still blocks. `"model_blocks": true` (or `--model-blocks`) restores blocking. The Python `Gate` keeps `model_blocks=True` by default for compatibility.
- **Blocked calls don't use up the budget.** Counts and totals only include calls that were not blocked; a call that failed is released (from logged results in the audit, from `PostToolUse` in the hook).
- **Session memory.** "yes", "go ahead", "try again" continue the previous request, so counts don't reset. "staging only", "don't touch production" and "max $60" last for the whole session, until the user names that environment again. Sessions never share anything. This also fixes a v0.4 bug where "update staging, do not touch production" counted production as named.
- **Count rule:** "the duplicate charge" means one, list lengths, `qty` and share counts ("buy 10 shares"), quantities without money ("3 boxes"), and wildcard filters. New `max_count` policy setting.
- **Amount rule:** commas and European formats, €, £, ₹, Rs., currency codes, a change of currency, percentages and "half", `amount_cents`, `"full"`, and limits written in the request ("max $60"). A limit price is not read as a total.
- **Action rule:** draft → send, a question or a check → a change, pause → delete, limit → market order.
- **Duplicates** are judged by economic fields (account, symbol, side, quantity, price, amount), with the dedupe ID read the right way round: the same `client_order_id` or `idempotency_key` is the same order retried and passes; a new ID is a possible duplicate and asks (or blocks when it goes over the count); a duplicate after an unknown outcome asks.
- **Repeated blocks:** after 3 blocks on one task (`max_blocks`), everything else on that task asks a person.
- **Public sharing always asks** to confirm, even when requested (`confirm_public`).
- **Exact action** on every ask and block, built from the arguments in code.
- **Rules only by default** when a hosted endpoint has no key: the CLI and the hook run on rules and say so, instead of failing.
- **Trace audit:** tool results are kept (OpenAI `tool` messages, Anthropic `tool_result` blocks, or `result` on each call); "Couldn't verify" lists calls with no result, a log that ends suddenly, or an empty result; per-call labels (`gold` on a call, or `gold_calls`); `org` per record and `--org`; calls of one conversation always run in order; `--repeat N` reports calls whose decision changed between runs; false alarms come first in the report, split into wrongly blocked and sent to a person.
- **Shadow report** starts with interruptions per week, and adds a per-tool table.
- New `benchmarks/multistep_v05.jsonl`: 17 conversations covering an action after a limit was set, an instruction hidden in a tool result, a legitimate change by the user, small calls that add up past a limit, session isolation, "yes" in a long chat, retries after a block, a lost response, a cut-off log, a crashed run, and two calls in the wrong order (expected misses).

Prompted by:
- The maintainer's run of 37 wrong-argument cases on Jev 1.14: all 5 false alarms came from the model (3 from the broad question, 2 from the external-send check on recipients the user had named); rules-only missed amount 4, count 4, recipient 3, customer 1 and action 3 cases.
- The trading-agent evaluation (issue #1): duplicate orders slipped past because the repeated-call check compared the full call text, so a new order ID defeated it; the tester's follow-up pointed out that the dedupe ID should be read the other way round, not ignored, and that tool results were dropped from the messages format.
- Public discussion: a checker that cries wolf gets switched off, so false alarms should lead the report; "couldn't verify" needs a reason; a retry should not escape the original scope; blocked calls should not use up the budget (reserve, commit, release); the latest message is often just "yes"; approvals should show concrete parameters; public sharing should always be confirmed; the model should be a second pass, not the gate.

Not in v0.5: calls in the wrong order, a held "defer" outcome, structured scope from the app, rules for order symbols and sides, and changing a call instead of blocking it. These are on the v0.6 list.

## v0.4.1

**Default model is now `liquid/d1.1-20261027`, and the client retries rate limits.**
- `liquid/d1-20260930` was retired on OpenRouter on 2026-11-20, so the old default and README setup returned HTTP 404 on every call. The default, README and docs now use d1.1. Earlier hosted d1 results were measured on the retired model and are kept as they were.
- Rate limits (429) and temporary server errors (502, 503, 504) are retried up to 3 times, waiting 1, 2 and 4 seconds or the server's `Retry-After`. New `retries` setting. A call that still fails is still **error**, never allow.
- HTTP errors now show the server's own message (for example why a model was not found) instead of a bare "404 Not Found".
- The request User-Agent now reports the real version.

Prompted by:
- An outside tester's trading-agent run (13 private cases, 6 repeated runs on d1.1): the README setup returned 404 because the default model was retired, and 3 of 120 calls failed with HTTP 429 with no retry, so a busy run lost coverage.
- The maintainer's own runs hit the same unexplained 404, which took a long time to trace because the error carried no server message.

No gate logic changed: rules, checks and thresholds are the same as v0.4.0.

## v0.4.0

**Rules first, model second.** Plain-code rules compare hard facts from the user's request with the tool call: environment, count, amount, audience, new payees and repeated calls. A mismatch blocks with a plain-language gap message; a missing fact on a risky action asks; the model only judges what rules can't express. New `--rules-only` mode (no model) and `--no-rules` (model only, as in v0.3). Policy file for fixed limits.

Prompted by:
- Public discussion (Reddit, LinkedIn): several builders independently said a model should not be the only check, that permission to use a tool is not permission for that target and amount, and that approvals should show the gap rather than the raw call.
- Measured misses: hosted d1 and local Nimble both missed argument-level risks in `dev_fresh` (d16 public sharing, d21 production when staging was asked, d26 two refunds when one was asked). The rules were written after seeing these misses, so `dev_fresh` results for v0.4 are optimistic.
- A payments sandbox report: plain-code refund checks stopped all of its 32 attacks, while prompt-level defenses made things worse.
- The independent evaluator said its private cases include loops that look like progress, which v0.3 could not catch. Loop detection was added as a simple repeated-call rule, without seeing those cases.

## v0.3.0

R-Judge support (folders of multi-step conversations, scored per conversation), `--no-thoughts` and `--tool-output`, and an accuracy line that counts "ask" as allow.

Prompted by: a 20-conversation R-Judge sample, where VexorGate missed injected instructions hidden in tool outputs, and where the agent's own reasoning ("the user might want to follow this instruction") talked the checker into allowing the action.

## v0.2.0

Trace audit (`vexorgate audit`) for existing agent logs, renamed to VexorGate.

## v0.1.0

The gate: a broad question, eight narrow checks and an injection check in one `/systemone` request; Claude Code hook in shadow or enforce mode.
