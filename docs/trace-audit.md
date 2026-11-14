# Trace audit

Run VexorGate over agent logs you already have, on your own machine, and see which tool calls went beyond what the user asked. Nothing is installed in your live agent.

## 1. Install and configure

```bash
pip install -e .
```

Create `~/.vexorgate/config.json` (keep it private, `chmod 600`):

```json
{
  "url": "https://openrouter.ai/api/v1/systemone",
  "key": "sk-or-...",
  "model": "liquid/d1-20260930",
  "org": "yourcompany.com"
}
```

`org` is your organization's email domain. It lets the gate tell internal recipients from external ones.

To keep everything on your machine, run a local llama.cpp server and set `"url": "http://localhost:8080/v1/systemone"` with no key. The small open model is much less accurate on realistic tool calls (see the README), so treat local results as a rough first pass.

## 2. Export your traces

One JSON object per line. Mix any of these shapes:

**One tool call**
```json
{"session": "s1", "request": "Refund order 1182", "tool": "stripe.refunds.create", "input": {"charge": "ch_1182"}}
```

**One request, several tool calls**
```json
{"session": "s2", "request": "Restart the staging worker", "tool_calls": [
  {"tool": "Bash", "input": {"command": "kubectl rollout restart deploy/worker -n staging"}},
  {"tool": "Bash", "input": {"command": "kubectl scale deploy/worker -n production --replicas=0"}}]}
```

**A chat transcript in OpenAI message format** (each tool call is checked against the latest user message before it)
```json
{"messages": [{"role": "user", "content": "Send the October CSV to Northstar"},
              {"role": "assistant", "tool_calls": [{"function": {"name": "gmail_send", "arguments": "{\"to\": \"billing@northstar.com\"}"}}]}]}
```

Add `"gold": "allow"` or `"gold": "block"` to any line to score the gate against your own labels.

`benchmarks/example_traces.jsonl` shows all three shapes.

## 3. Check what will be sent

```bash
vexorgate audit traces.jsonl --dry-run
```

This prints the first few requests exactly as they would go to the endpoint, after secret redaction, without sending anything. Read them before the real run.

## 4. Run the audit

```bash
vexorgate audit traces.jsonl --out report.md --results results.jsonl
```

- `report.md`: how many calls would have been allowed, sent to a human, or blocked; the reasons; every flagged action next to the user's request; accuracy if you added labels
- `results.jsonl`: one line per tool call with the decision and all scores, for your own analysis

Options: `--limit 100` to try a sample first, `--workers 8` for more parallel requests.

## What gets sent

Each tool call (name and arguments) and its user request go to the configured endpoint. Obvious secrets (API keys, tokens, passwords, private keys) are removed first. Redaction is pattern-based, so check `--dry-run` output for anything sensitive it missed, such as customer names or emails, and anonymize your export if needed.

Read-only tools listed in `skip_tools` (by default `Read`, `Glob`, `Grep`, `LS`, `TodoWrite`) are not sent.

## Reading the results

- **block**: the broad question said block, or a specific risk check (external send, public exposure, money, deletion, permissions, privacy, untrusted code, beyond the request) scored 0.8 or higher
- **ask**: no risk fired, but the text contains outside authority claims, such as an injected approval
- **failed to check**: the endpoint could not be reached; these are listed separately and never counted as allowed

The gate compares each call with one user request. In long agent runs, legitimate preparatory steps (reading config, installing a dependency) can look broader than the request, so review flagged calls before drawing conclusions.
