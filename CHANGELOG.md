# Changelog

Each change notes what prompted it, so results can be read next to the reason for the change.

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
