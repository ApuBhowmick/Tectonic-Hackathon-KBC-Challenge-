"""Load the YAML configuration. Config is passed around explicitly (no module-level state)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from engine.db import REPO_ROOT

CONFIG_DIR = REPO_ROOT / "config"


@dataclass(frozen=True)
class Config:
    events: dict[str, dict[str, Any]]   # event_type -> {description, signals, needs}
    policy: dict[str, Any]

    @property
    def lookback_days(self) -> int:
        return int(self.policy["lookback_days"])

    def need_catalog(self, event_type: str) -> dict[str, Any]:
        return self.events.get(event_type, {}).get("needs", {})


def _yaml(name: str, config_dir: Path) -> dict[str, Any]:
    with open(config_dir / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_config(config_dir: Path | None = None) -> Config:
    d = config_dir or CONFIG_DIR
    events = _yaml("life_events.yaml", d)
    events.pop("triage", None)
    return Config(events=events, policy=_yaml("policy.yaml", d))


def load_demo(config_dir: Path | None = None) -> dict[str, Any]:
    return _yaml("demo.yaml", config_dir or CONFIG_DIR)


def demo_as_of_default(config_dir: Path | None = None) -> date:
    v = load_demo(config_dir)["as_of_default"]
    return v if isinstance(v, date) else date.fromisoformat(str(v))
