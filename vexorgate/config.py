"""Configuration: environment variables override ~/.vexorgate/config.json, which overrides defaults."""
import json
import os
from pathlib import Path

DEFAULTS = {
    "url": "https://openrouter.ai/api/v1/systemone",
    "key": "",
    "model": "liquid/d1-20260930",
    "org": "",
    "mode": "shadow",            # shadow: log only, never interfere. enforce: answer ask/deny.
    "timeout": 8,
    "skip_tools": ["Read", "Glob", "Grep", "LS", "TodoWrite"],
}

ENV = {
    "url": "VEXORGATE_URL",
    "key": "VEXORGATE_KEY",
    "model": "VEXORGATE_MODEL",
    "org": "VEXORGATE_ORG",
    "mode": "VEXORGATE_MODE",
}


def home() -> Path:
    return Path(os.environ.get("VEXORGATE_HOME") or Path.home() / ".vexorgate")


def load() -> dict:
    cfg = dict(DEFAULTS)
    f = home() / "config.json"
    if f.exists():
        cfg.update(json.loads(f.read_text()))
    for name, var in ENV.items():
        if os.environ.get(var):
            cfg[name] = os.environ[var]
    cfg["key"] = str(cfg.get("key", "")).strip()
    cfg["mode"] = str(cfg.get("mode", "shadow")).strip().lower()
    return cfg
