"""Configuration loader.

Loads config/config.yaml into a lightweight dot-accessible object. No magic:
a thin wrapper over a dict so downstream code reads `cfg.risk.vol_target_annual`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


class DotDict(dict):
    """dict with attribute access; nested dicts are wrapped lazily."""

    def __getattr__(self, name: str) -> Any:
        try:
            value = self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        if isinstance(value, dict) and not isinstance(value, DotDict):
            value = DotDict(value)
            self[name] = value
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> DotDict:
    with open(path, "r") as fh:
        raw = yaml.safe_load(fh)
    return DotDict(raw)


# Singleton for convenience
CONFIG = load_config() if DEFAULT_CONFIG_PATH.exists() else DotDict()
