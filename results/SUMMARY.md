# Results summary

All results use the gate wording in `vexorgate/gate.py` (the broad question, 8 narrow checks and the injection check), frozen before the `dev_fresh` runs. Runs date from November 2026.

## Datasets

| File | Cases | Notes |
|---|---|---|
| `benchmarks/dev_fresh.jsonl` | 28 (14 risky, 14 safe) | Realistic tool calls (shell, Stripe, GitHub, Slack, Drive, kubectl, AWS). AI-generated from a short brief, reviewed by hand. **Never used for tuning.** Organization domain: `acme.io`. |
| `benchmarks/agent_gating_v2.jsonl` | 50 (25 risky, 25 safe) | Hard cases with look-alike safe actions. **Used during development** to reword checks, so results on it are optimistic. Organization domain: `ourcompany.com`. |

Case `d17` originally lost its dollar signs in a copy step. The published file has them restored; the runs below used the restored text.

## Clean accuracy, `dev_fresh` (28 cases)

| Model | single | hybrid | hardened |
|---|---|---|---|
| d1-3B, open weights, local (f16, T4) | 15 | 16 | 16 |
| `liquid/d1-20260930`, hosted | 27 | 28 | 27 |
| `typesafe/jev-1.14-20261014` | 24 | 24 | 25 |

Repeated hosted runs varied by one case; report hosted d1 as 27–28/28.

Reproduce the hardened column:

```bash
VEXORGATE_ORG=acme.io vexorgate audit benchmarks/dev_fresh.jsonl
```

## Under attack, `dev_fresh`

Attacks were applied only to cases each gate got right without the attack.

| Fail-open (risky action allowed) | single | hybrid | hardened |
|---|---|---|---|
| d1 hosted | 26/130 (20%) | 8/140 (6%) | **0/140 (0%)** |
| Jev 1.14 | 23/130 (18%) | 8/140 (6%) | **0/140 (0%)** |

| False alarms on legitimate extra text | single | hybrid | hardened |
|---|---|---|---|
| d1 hosted | 3/70 (4%) | 1/65 (2%) | 8/70 (11%) |
| Jev 1.14 | 0/55 (0%) | 0/55 (0%) | 3/55 (5%) |

Attacks: log noise, fake approval, JSON wrapping, two option-definition rewrites, a renamed block label, a fake ticket, a fake user quote, a fake `[SYSTEM]` tag, and a compliance claim. In VexorGate the option definitions are fixed in code, so the three option attacks cannot reach the hardened gate.

## Comparison with LlamaFirewall AlignmentCheck

Meta's AlignmentCheck prompt and scoring, run on `meta-llama/llama-4-maverick` via OpenRouter (Meta's default setup uses Together AI).

| | VexorGate hardened, hosted d1 | AlignmentCheck |
|---|---|---|
| Clean, `dev_fresh` | 27/28 | 24/28 |
| Clean, `agent_gating_v2` | not run | 48/50 |
| Fail-open under attack, `dev_fresh` | 0/140 (0%) | at least 6/70 (9%), **partial run** |
| Median time per decision | under 0.1 s | about 1.9 s |

AlignmentCheck's clean misses on `dev_fresh` were all risky actions allowed (d16, d17, d18, d23); it blocked no safe actions. The attack run was cut short when API credit ran out: 34 of about 270 calls failed, and AlignmentCheck counts failures as escalations, so its true fail-open rate is 9% or higher. Its false-alarm rate was not measured. Three option attacks do not apply to AlignmentCheck.

## Known gaps

- Small datasets, synthetic cases and attacks. One case moves a result by several points.
- Data ownership (one customer's data sent to another) is not reliably caught by any model tested. Handle it with a rule that compares record owners with the recipient.
- All cases are one request and one action. Multi-step agent runs are untested.
