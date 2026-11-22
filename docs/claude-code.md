# VexorGate in Claude Code (shadow mode)

VexorGate watches every tool call Claude Code makes, checks it against what you asked, and logs what it *would* have done. In shadow mode it never blocks or changes anything.

## 1. Install

```bash
git clone <this repo> && cd vexorgate
pip install -e .
```

Use the same Python that `python3` runs in your terminal. On Windows, use `python` instead of `python3` below.

## 2. Configure

Create `~/.vexorgate/config.json`:

```json
{
  "url": "https://openrouter.ai/api/v1/systemone",
  "key": "sk-or-...",
  "model": "liquid/d1.1-20261027",
  "org": "yourcompany.com",
  "mode": "shadow"
}
```

Keep this file private (`chmod 600 ~/.vexorgate/config.json`). Environment variables (`VEXORGATE_URL`, `VEXORGATE_KEY`, `VEXORGATE_MODEL`, `VEXORGATE_ORG`, `VEXORGATE_MODE`) override it.

To keep tool calls on your machine, point `url` at a local llama.cpp server instead (`http://localhost:8080/v1/systemone`) and leave `key` empty. Expect lower accuracy with the small open model.

Check that it works:

```bash
vexorgate check --request "refund order 1182" --tool Bash --input '{"command": "stripe refunds create --all"}'
```

You should see `"decision": "block"` with a reason such as `beyond`.

## 3. Register the hooks

Add this to `~/.claude/settings.json` (all projects) or `.claude/settings.json` (one project):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "python3 -m vexorgate.claude_hook" } ] }
    ],
    "PreToolUse": [
      { "matcher": "*", "hooks": [ { "type": "command", "command": "python3 -m vexorgate.claude_hook", "timeout": 10 } ] }
    ]
  }
}
```

Restart Claude Code and work as usual.

## 4. Read the report

```bash
vexorgate report --days 7
```

It shows how many tool calls the gate would have allowed, sent to you for confirmation, or blocked, the most common reasons, and each action it would have stopped, with your original request next to it. Raw logs are in `~/.vexorgate/logs/`.

## What gets sent and stored

- Each checked tool call (tool name and arguments) and your latest request go to the configured endpoint.
- Obvious secrets (API keys, tokens, passwords, private keys) are redacted first. Redaction is pattern-based, so review the logs before sharing them.
- Read-only tools (`Read`, `Glob`, `Grep`, `LS`, `TodoWrite`) are skipped by default. Change `skip_tools` in the config to adjust.

## Turning on enforcement (later)

Set `"mode": "enforce"`. Then:
- **block** → Claude Code denies the tool call and shows the reason, as a gap message when a rule fired ("You asked for staging. The agent targets production.")
- **ask** → Claude Code asks you to confirm
- **check failed** → Claude Code asks you to confirm

Run shadow mode for at least a week first, and read the report, so you know the false-alarm rate on your own work.

Counts, totals and repeated calls are remembered per Claude Code session, so a second refund or a third identical command is caught.

To run without any model, add `"rules_only": true` to the config.

## Known limits

- The gate compares each tool call with your **latest** request. In long sessions, earlier instructions may not be taken into account.
- Hooks run before each tool call, so every checked call waits for the gate, usually well under a second with hosted d1.
