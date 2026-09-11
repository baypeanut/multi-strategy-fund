"""Shared offline-suite fixtures.

E31 root-cause fix. The offline suite must never touch the real IB Gateway, but
it silently did: the tick-loop entry gates broker sync on
`CONFIG["ibkr"]["enabled"]`, and the live config ships that flag TRUE. So every
test that runs a full `_tick()` (halt-latch, guard, info-gate, s5) attempted a
real bounded broker connect and paid ib_async's ~25s connect timeout per
attempt — multi-tick tests stacked it into 75-183s calls, and the whole suite's
wall-clock became a function of whether the Gateway happened to refuse fast or
hang. That variance (82s idle vs 518s under a hanging Gateway) is exactly what
blew the sandbox's timeout wall and stranded P0009 as a false ❌.

Disabling the flag for the offline suite collapses it back to seconds and makes
it deterministic. The dedicated broker tests (`test_ibkr*.py`) inject a
CapturingBroker and call `_mirror_to_ibkr` / `_refresh_ibkr` directly, so they
never read this flag and are unaffected — verified: no test asserts IBKR
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
    every `_run_systems` test reach SEC EDGAR live through `_run_s5` —
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
    `auto_restart`, both reading the LIVE config — so any test exercising the
    stale-link path (P0006/P0010's, written before the power existed and
    therefore not patching it) issued a real `sudo systemctl restart
    ib-gateway`. Observed 2026-07-28: 98 real restarts, ~6 per suite run, 10s
    apart, DURING market hours — every full-suite run, including the ones the
    apply gate runs unattended at 04:30.

    Belt and braces, but surgical: the config flags go off, AND a subprocess
    guard rejects `systemctl restart` specifically — so a future test that
    builds its own config still cannot reach systemd, while the helpers stay
    fully testable (tests patch `subprocess.run` themselves, and a later
    monkeypatch wins over this fixture's)."""
    monkeypatch.setitem(CONFIG.setdefault("watchdog", {}),
                        "auto_restart_gateway", False)
    monkeypatch.setitem(CONFIG.setdefault("agent_autonomy", {}),
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

    `nightly.main()` imports `run_engineer` INSIDE the function, so patching
    `research.nightly.run_engineer` does nothing — and on a machine with a real
    key in .env, a test that called `main()` fired a REAL pipeline session:
    Fable planning at max effort plus parallel Opus 5 engineers, killed only by
    the 90s pytest timeout after burning the budget. Three suite runs cost
    three sessions.

    Blocking at the credential boundary is the narrowest fix that cannot be
    bypassed by an inner import: with no key, `run_engineer` and `run_director`
    both return before constructing a client. Tests that exercise those paths
    patch `get_key` themselves, and a later monkeypatch wins over this one."""
    import research.director as director
    import research.engineer as engineer

    def no_key(name: str):
        return None if name == "ANTHROPIC_API_KEY" else _real_get_key(name)

    from core.env import get_key as _real_get_key
    for mod in (engineer, director):
        monkeypatch.setattr(mod, "get_key", no_key, raising=False)


@pytest.fixture(autouse=True)
def _no_live_record_writes(monkeypatch, tmp_path_factory):
    """E53, sixth instance of the class: the offline suite must not mutate
    production records.

    E31 dialled the real Gateway, E35 fetched EDGAR live, E38 restarted systemd,
    E46 spent real model budget. This one was mine. `retire_stale_proposals()`
    was placed before the backpressure check inside `run_engineer`, and
    `test_the_suite_cannot_spend_money` calls `run_engineer` with `pending_count`
    patched but nothing standing in front of the retirement. One suite run on
    the server retired all four real proposals in the live store.

    E63e: the guard alone was not enough, and it produced the opposite failure.
    Three tests reach `run_engineer` legitimately, so on the BOX - where the
    live store holds real records - the guard fired and the suite went red, and
    stayed red for days. The identical tree was green anywhere the store was
    empty. A test whose outcome depends on what is sitting in production is not
    a test, and a permanently red suite destroys the ability to notice anything
    new (E55b: it also disarms every apply gate).

    So redirect FIRST and guard SECOND, which is what the guard's own error
    message told callers to do. The suite now never points at the live store, so
    the guard never needs to fire; it stays as the backstop for anything that
    puts PROPOSALS_DIR back.
    """
    import research.engineer as engineer

    real_store = engineer.PROPOSALS_DIR
    real_save = engineer.save_record
    sandbox = tmp_path_factory.mktemp("proposals")
    monkeypatch.setattr(engineer, "PROPOSALS_DIR", sandbox, raising=False)

    def guarded(rec):
        if engineer.PROPOSALS_DIR == real_store:
            raise AssertionError(
                f"the offline suite tried to write proposal {rec.get('id')} to "
                f"the LIVE store at {real_store}; redirect PROPOSALS_DIR or "
                f"patch save_record in the test that needs it")
        return real_save(rec)

    monkeypatch.setattr(engineer, "save_record", guarded, raising=False)


@pytest.fixture(autouse=True)
def _no_live_telegram(monkeypatch):
    """E67, eighth instance of the class: the offline suite must not message the
    owner's phone.

    maybe_send_daily_digest lives in core.alerts and calls the module-level
    send_telegram THERE. Tests that tick a LiveRuntime patched
    `runtime.live.send_telegram` (or `live`/`live_mod`), a different reference,
    so the digest path went out over the real bot token whenever a test ticked
    with the wall clock past hour_utc. Repeated full-suite runs ON THE BOX, where
    .env holds the real token, flooded the owner with fresh-state $3M "daily
    summary" messages - Ticks=1, eq=0%, spy=MISSING - which is the offline suite
    touching a live system, same class as E31/E35/E38/E46/E53.

    Blocked at the DEFINITION, core.alerts.send_telegram, which no per-test
    patch of a re-exported name can bypass. Tests that assert on telegram
    behaviour (test_alerts.py) patch core.alerts.send_telegram themselves and a
    later monkeypatch wins over this one.
    """
    import core.alerts as alerts

    def _blocked(*a, **k):
        return True
    _blocked._offline_guard = True          # identity marker, pinned below
    monkeypatch.setattr(alerts, "send_telegram", _blocked, raising=False)


@pytest.fixture(autouse=True)
def _no_real_baseline_run(monkeypatch):
    """E58b, seventh instance of the class: the offline suite must not do the
    expensive live thing.

    E58 put a real sandbox suite-run at the top of `run_engineer_until_settled`
    so the lane proves the apply gate would accept something before it buys a
    token. Correct for the nightly run, ruinous inside pytest: every test that
    reaches that function builds a repo copy and runs the whole suite inside
    itself. The settle-loop tests alone took the suite from four seconds to
    over three minutes, and the first version recursed without bound because
    the copied suite contained the test that made the copy.

    Patching it per test is whack-a-mole - it is reachable from the settle
    loop, from nightly.main and from anything that grows a call to either.
    Stubbed green here so no test ever pays for it; the three tests that
    exercise the gate patch it themselves, and a later monkeypatch wins."""
    import research.engineer as engineer

    monkeypatch.setattr(engineer, "baseline_is_green",
                        lambda *a, **k: {"green": True, "failed": [],
                                         "summary": "stubbed by the offline suite"},
                        raising=False)
