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

Local runs through Ollama, hardened gate:

| Model | Hardware | `dev_fresh` | `agent_gating_v2` | Median time |
|---|---|---|---|---|
| `nimble` (9B) | Kaggle T4 GPU | 23/28 (risky stopped 9/14, safe allowed 14/14) | 48/50 | 2.0 s |
| `d1:3b` | laptop CPU (8 GB RAM) | 16/28 (same 12 misses as the earlier Kaggle run) | not run | 16 s |

Nimble's `dev_fresh` misses: d16, d17, d19, d21, d23, all with the risk in the arguments. On `agent_gating_v2`, where risks are described in plain words, it missed only r18 and s16 (that set was used during development, so its scores are optimistic).

Reproduce the hardened column:

```bash
VEXORGATE_ORG=acme.io vexorgate audit benchmarks/dev_fresh.jsonl
```

## v0.4 rules only (no model)

| Set | Correct | Risky stopped | Safe allowed |
|---|---|---|---|
| `dev_fresh` | 19/28 | 5/14 (d16, d17 ask, d21, d26, d28) | 14/14 |
| `agent_gating_v2` | 31/50 | 6/25 | 25/25 |

The rules never blocked or questioned a safe case in either set. They were written after seeing the models' misses on `dev_fresh`, so treat these as optimistic. Combined with a model (rules first, then Nimble or hosted d1) has not been measured yet.

Reproduce:

```bash
VEXORGATE_ORG=acme.io vexorgate audit benchmarks/dev_fresh.jsonl --rules-only
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

## v0.5 rules only (no model)

| Set | Correct | Risky stopped | Safe allowed | Notes |
|---|---|---|---|---|
| `dev_fresh` (org `acme.io`) | 21/28 | 7/14 | 14/14 | v0.4: 19/28 |
| `agent_gating_v2` (org `ourcompany.com`) | 32/50 | 7/25 | 25/25 | v0.4: 31/50 |
| `multistep_v05`, per call | 33/35 | 12/14 | 21/21 | per record 15/17 |

Reproduce: `vexorgate audit benchmarks/dev_fresh.jsonl --rules-only --org acme.io` (and the same for the others; `multistep_v05` sets `org` per record).

`multistep_v05` was written for v0.5, from the failure kinds raised in public discussion and by the testers, so it shows the rules doing what they were built for, not how they generalize. Its two misses (m12, m13) are calls in the wrong order, left in on purpose. v0.5 has not yet been run with a model.

## Known gaps

- Small datasets, synthetic cases and attacks. One case moves a result by several points.
- Data ownership (one customer's data sent to another) is not reliably caught by any model tested. Handle it with a rule that compares record owners with the recipient.
- `dev_fresh` and `agent_gating_v2` are one request and one action. `multistep_v05` adds conversations, but all three are synthetic; real multi-step traffic is still largely untested.
