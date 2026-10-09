"""The reviewer demo is local, synthetic and independent of trading runtime."""

import json
from pathlib import Path
import stat
import subprocess
import sys
import threading

import pytest

from scripts import demo_dashboard as demo


def test_demo_import_has_no_model_or_trading_runtime_dependency():
    source = """
import sys
from scripts.demo_dashboard import make_demo_state
state = make_demo_state()
assert state['demo_metadata']['synthetic'] is True
assert not any(name in sys.modules for name in ('runtime.live', 'core.config', 'anthropic', 'httpx', 'ib_async'))
"""
    completed = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, timeout=10
    )
    assert completed.returncode == 0, completed.stderr


def test_demo_uses_private_temporary_state_and_loopback_only(monkeypatch):
    observed = {}

    class Server:
        def shutdown(self):
            observed["shutdown"] = True

        def server_close(self):
            observed["closed"] = True

    def start(state_path, *, host, port, demo_mode):
        path = Path(state_path)
        observed.update(path=path, host=host, port=port, mode=demo_mode)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        state = json.loads(path.read_text())
        assert state["demo_metadata"]["synthetic"] is True
        assert state["ibkr"]["account"] == "DEMO_ONLY"
        assert all(row["side"] == "SYNTHETIC" for row in state["ibkr"]["fills"])
        return Server()

    monkeypatch.setattr(demo, "start_dashboard", start)
    stop = threading.Event()
    stop.set()
    demo.run_demo(port=0, stop_event=stop)
    assert observed["host"] == "127.0.0.1" and observed["mode"] is True
    assert observed["shutdown"] and observed["closed"]
    assert not observed["path"].exists()


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf")])
def test_bad_demo_duration_never_starts_a_server(duration, monkeypatch):
    monkeypatch.setattr(
        demo, "start_dashboard", lambda *a, **kw: pytest.fail("invalid demo must not bind")
    )
    with pytest.raises(ValueError):
        demo.run_demo(duration=duration)
