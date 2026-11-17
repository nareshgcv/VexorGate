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
    "rules": True,               # hard-fact rules first (vexorgate.facts)
    "rules_only": False,         # true: no model at all, nothing leaves the machine
    "policy": {
        "sensitive_envs": ["production"],   # touching these without being asked: ask, or block on mismatch
        "max_amount": None,                 # money above this always asks, whatever the request says
        "always_ask_tools": [],             # tool names (or prefixes ending in *) that always ask
        "loop_threshold": 3,                # same call on the same target this many times: block
    },
}

ENV = {
    "url": "VEXORGATE_URL",
    "key": "VEXORGATE_KEY",
    "model": "VEXORGATE_MODEL",
    "org": "VEXORGATE_ORG",
    "mode": "VEXORGATE_MODE",
    "rules_only": "VEXORGATE_RULES_ONLY",
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
    if isinstance(cfg.get("rules_only"), str):
        cfg["rules_only"] = cfg["rules_only"].strip().lower() in ("1", "true", "yes", "on")
    policy = dict(DEFAULTS["policy"])
    policy.update(cfg.get("policy") or {})
    cfg["policy"] = policy
    return cfg
