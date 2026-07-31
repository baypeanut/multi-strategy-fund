"""Shared offline-suite fixtures.

E31 root-cause fix. The offline suite must never touch the real IB Gateway, but
it silently did: the tick-loop entry gates broker sync on
`CONFIG["ibkr"]["enabled"]`, and the live config ships that flag TRUE. So every
test that runs a full `_tick()` (halt-latch, guard, info-gate, s5) attempted a
real bounded broker connect and paid ib_async's ~25s connect timeout per
attempt - multi-tick tests stacked it into 75-183s calls, and the whole suite's
wall-clock became a function of whether the Gateway happened to refuse fast or
hang. That variance (82s idle vs 518s under a hanging Gateway) is exactly what
blew the sandbox's timeout wall and stranded P0009 as a false ❌.

Disabling the flag for the offline suite collapses it back to seconds and makes
it deterministic. The dedicated broker tests (`test_ibkr*.py`) inject a
CapturingBroker and call `_mirror_to_ibkr` / `_refresh_ibkr` directly, so they
never read this flag and are unaffected - verified: no test asserts IBKR
behavior *through* the tick loop.
"""
import pytest

from core.config import CONFIG


@pytest.fixture(autouse=True)
def _no_live_broker(monkeypatch):
    ibkr = CONFIG.setdefault("ibkr", {})
    monkeypatch.setitem(ibkr, "enabled", False)


@pytest.fixture(autouse=True)
def _no_live_s5_feed(monkeypatch):
    """E35 twin of the broker fix above: flipping s5_event.enabled=true made
    every `_run_systems` test reach SEC EDGAR live through `_run_s5`
    fast-and-green on one network, a 90s timeout on another. Same rule as the
    Gateway: the offline suite never touches the network. The S5 engine tests
    construct S5EventEngine directly and never read this flag."""
    s5 = CONFIG.setdefault("systems", {}).setdefault("s5_event", {})
    monkeypatch.setitem(s5, "enabled", False)


@pytest.fixture(autouse=True)
def _no_live_restarts(monkeypatch):
    """E38, third instance of the same class: the offline suite must not
    operate live systemd units.

    E34 gave the watchdog `auto_restart_gateway` and the apply gate
    `auto_restart`, both reading the LIVE config - so any test exercising the
    stale-link path (P0006/P0010's, written before the power existed and
    therefore not patching it) issued a real `sudo systemctl restart
    ib-gateway`. Observed 2026-07-28: 98 real restarts, ~6 per suite run, 10s
    apart, DURING market hours - every full-suite run, including the ones the
    apply gate runs unattended at 04:30.

    Belt and braces, but surgical: the config flags go off, AND a subprocess
    guard rejects `systemctl restart` specifically - so a future test that
    builds its own config still cannot reach systemd, while the helpers stay
    fully testable (tests patch `subprocess.run` themselves, and a later
    monkeypatch wins over this fixture's)."""
    monkeypatch.setitem(CONFIG.setdefault("watchdog", {}),
                        "auto_restart_gateway", False)
    monkeypatch.setitem(CONFIG.setdefault("agent_governance", {}),
                        "auto_restart", False)

    import subprocess
    from types import SimpleNamespace
    real_run = subprocess.run

    def guarded_run(cmd, *a, **kw):
        flat = " ".join(str(c) for c in cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "systemctl" in flat and "restart" in flat:
            return SimpleNamespace(returncode=1, stdout="",
                                   stderr="restart blocked by the offline test suite")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", guarded_run)


@pytest.fixture(autouse=True)
def _no_live_llm(monkeypatch):
    """E46, fourth instance of the class: the offline suite must not spend money.

    A test that reached an agent entry point on a machine with a real key in
    .env fired a REAL model session and burned budget, killed only by the 90s
    pytest timeout. Three suite runs cost three sessions.

    Blocking at the credential boundary is the narrowest fix, and the one an
    inner import cannot bypass: with no key the agents return before they
    construct a client. Tests that exercise those paths patch `get_key`
    themselves, and a later monkeypatch wins over this one."""
    import research.director as director
    import research.security_audit as security_audit
    from core.env import get_key as _real_get_key

    def no_key(name: str):
        return None if name == "ANTHROPIC_API_KEY" else _real_get_key(name)

    for mod in (director, security_audit):
        monkeypatch.setattr(mod, "get_key", no_key, raising=False)
