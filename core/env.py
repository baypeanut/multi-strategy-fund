"""Central .env / environment key loading.

Single source of truth for API credentials. Keys live in the repo-root .env on
the server (0600, never committed) or in real environment variables; env vars
win. Every provider asks here — presence of a key is what activates paid
integrations (Anthropic, Polygon, Sharadar), so the system upgrades itself the
moment a key lands in .env, and degrades gracefully when absent.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def get_key(name: str) -> str | None:
    """Environment variable first, then .env file. None if absent/empty."""
    val = os.environ.get(name) or load_env_file().get(name)
    return val or None


def has_key(name: str) -> bool:
    return get_key(name) is not None
