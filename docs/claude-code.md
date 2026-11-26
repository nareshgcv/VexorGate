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
      { "matcher": "*", "hooks": [ { "type": "command", "command": "python3 -m vexorgate.claude_hook", "timeout": 20 } ] }
    ],
    "PostToolUse": [
      { "matcher": "*", "hooks": [ { "type": "command", "command": "python3 -m vexorgate.claude_hook" } ] }
    ]
  }
}
```

`PostToolUse` is optional. It tells VexorGate whether the last money action failed, so a corrected retry is not counted twice.

Keep the `PreToolUse` timeout above VexorGate's own time limit for a check (`"deadline"`, 15 seconds by default, retries included). If a hook runs past Claude Code's timeout, Claude Code may run the tool without an answer.

**It fails closed in enforce mode.** Claude Code runs a tool when its hook gives no answer, so any problem inside VexorGate (a damaged session file, a broken `config.json`, an unexpected input, or another hook holding the session for more than 10 seconds) answers "ask" with the reason, instead of staying silent. A damaged session file is set aside as `*.damaged-*.json`, and the next call asks once, because earlier counts in that session were lost. In shadow mode, errors are only logged.

**Files stay private.** `~/.vexorgate/` and its `sessions/` and `logs/` folders are readable only by you (on macOS and Linux), prompts are redacted before they are saved, and session files are written all at once with a lock, so parallel tool calls don't lose each other's counts.

Restart Claude Code and work as usual.

## 4. Read the report

```bash
vexorgate report --days 7
```

It starts with how often the gate would have interrupted you per week, then shows how many tool calls it would have allowed, sent to you for confirmation, or blocked, per tool, the most common reasons, and each action it would have stopped, with your original request and the exact action next to it. Raw logs are in `~/.vexorgate/logs/`.

## What gets sent and stored

- Each checked tool call (tool name and arguments) and your latest request go to the configured endpoint.
- Obvious secrets (API keys, tokens, passwords, private keys) are redacted first. Redaction is pattern-based, so review the logs before sharing them.
- Read-only tools (`Read`, `Glob`, `Grep`, `LS`, `TodoWrite`) are skipped by default. Change `skip_tools` in the config to adjust.

## Turning on enforcement (later)

Set `"mode": "enforce"`. Then:
- **block** → Claude Code denies the tool call and shows the reason, as a gap message when a rule fired ("You asked for staging. The agent targets production."), and the exact action
- **ask** → Claude Code asks you to confirm. A flag from the model alone asks rather than blocks, unless you set `"model_blocks": true`
- **check failed** → Claude Code asks you to confirm

Run shadow mode for at least a week first, and read the report, so you know the false-alarm rate on your own work.

Counts, totals and repeated calls are remembered per Claude Code session, so a second refund or a third identical command is caught. Replying "yes" or "go ahead" continues your previous request instead of replacing it, and limits you set earlier ("staging only", "don't touch production") last for the whole session until you name that environment again. After 3 blocks on one task, everything else on that task asks you.

To run without any model, add `"rules_only": true` to the config. If no key is set for a hosted endpoint, the hook runs on rules only.

## Known limits

- The gate remembers your current request and the limits you set, but it reads them with keyword rules. An instruction phrased in an unusual way may not be picked up as a limit.
- Hooks run before each tool call, so every checked call waits for the gate, usually well under a second with hosted d1.
