"""E59: the recorded data provider answered from the key, not from reality.

`make_equity_provider` tries Polygon when the key is present and silently falls
back to yfinance on ANY construction failure. `equity_provider_name` answered
from `has_key("POLYGON_API_KEY")`. So a Polygon outage left the fund pricing
off yfinance while state.data_provider, the dashboard and the research record
all said "polygon" - wrong provenance on every price in the book, with nothing
to show it.

Same shape as the realised-vol disagreement that cost 3.8 points on S4: one
fact, derived twice, from two different things.
"""
from core.data.factory import equity_provider_name, make_equity_provider


class _Boom:
    def __init__(self):
        raise RuntimeError("polygon down")


def test_the_name_comes_from_the_object_not_the_key(monkeypatch):
    from core.data.equities import EquityDataProvider
    monkeypatch.setattr("core.data.factory.has_key", lambda k: True)
    assert equity_provider_name(EquityDataProvider()) == "yfinance", (
        "the key said polygon; the object is yfinance and the object wins")


def test_a_polygon_outage_is_recorded_as_yfinance(monkeypatch):
    """The exact scenario: key present, construction fails, fund runs on the
    fallback. What gets recorded must be the fallback."""
    monkeypatch.setattr("core.data.factory.has_key", lambda k: True)
    import core.data.polygon as poly
    monkeypatch.setattr(poly, "PolygonDataProvider", _Boom)
    provider = make_equity_provider()
    assert equity_provider_name(provider) == "yfinance"


def test_every_provider_declares_its_own_name():
    from core.data.equities import EquityDataProvider
    from core.data.polygon import PolygonDataProvider
    assert EquityDataProvider.PROVIDER_NAME == "yfinance"
    assert PolygonDataProvider.PROVIDER_NAME == "polygon"


def test_an_unknown_provider_still_reports_something_usable():
    class Custom: ...
    assert equity_provider_name(Custom()) == "Custom", (
        "a provider without a declared name must still be identifiable, not "
        "silently reported as one of the known two")


def test_the_keyless_form_survives_for_callers_with_no_object():
    monkeypatch_free = equity_provider_name()
    assert monkeypatch_free in ("polygon", "yfinance")


# --- E63: the semantics half. A name is not a basis. --------------------------
def _runtime(tmp_path, monkeypatch, provider_of_record):
    """A runtime whose book was built on `provider_of_record`, with a healthy
    light tick so nothing ELSE can be the reason it refuses to trade."""
    import pandas as pd

    import runtime.live as live_mod
    from runtime.live import LightData, LiveRuntime

    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))
    rt.cache_path = tmp_path / "cache" / "hist.pkl"
    rt.state["data_provider"] = provider_of_record
    rt.state["systems"]["s4"]["weights"] = {"AAPL": 0.02}
    rt.state["prev_prices"] = {"AAPL": 100.0}
    monkeypatch.setattr(live_mod, "send_telegram", lambda *a, **k: True)
    monkeypatch.setattr(
        LiveRuntime, "_fetch_light",
        lambda self: LightData(bar_date="2026-08-10", prices={"AAPL": 101.0},
                               eq_cov=1.0, cx_cov=1.0, spy_close=500.0))
    # a new bar, so the tick genuinely WANTS to rebalance
    monkeypatch.setattr(LiveRuntime, "_should_rebalance",
                        lambda self, bar, raw: (True, "new bar"))
    # A healthy light tick opens the news path, and news ingest reaches Yahoo
    # RSS and EDGAR. Measured while writing this file: 31s for three tests
    # against a 5s suite. That is the E31/E35/E38/E46/E53 class - the offline
    # suite must not touch the network - and it would have been the eighth
    # instance. Stubbed here rather than in conftest because only these tests
    # drive a full tick with coverage healthy.
    monkeypatch.setattr(LiveRuntime, "_ingest_news",
                        lambda self, syms, now: None)
    monkeypatch.setattr(LiveRuntime, "_news_raw", lambda self, now: {})
    monkeypatch.setattr(LiveRuntime, "_backfill_s3_forward",
                        lambda self, bar, prices: None)
    rt._pd = pd
    return rt


def test_a_silent_provider_switch_produces_a_no_trade_tick(tmp_path, monkeypatch):
    """Polygon returns split-adjusted prices; yfinance with auto_adjust=True
    returns dividend-adjusted. make_equity_provider swallows any Polygon
    failure and hands back yfinance, so the fund's DEFINITION OF RETURN can
    change with no deploy and no announcement.

    Marking is fine either way - both leave today's close alone. Signals are
    not: 12-1 momentum reads a 252-day panel, and dividend-adjusted history
    sits lower in the past, so the cross-section re-ranks on a basis the record
    was never built on. E59 pinned the recorded NAME; this pins the semantics.

    The response reuses the coverage guard rather than adding anything: hold,
    keep marking, no turnover."""
    from runtime.live import LiveRuntime

    rt = _runtime(tmp_path, monkeypatch, provider_of_record="polygon")
    # the tick prices off yfinance while the book was built on polygon
    monkeypatch.setattr(LiveRuntime, "_fetch_heavy",
                        lambda self: (_ for _ in ()).throw(
                            AssertionError("heavy fetch must not run: it caches "
                                           "its panel, so a wrong-basis panel "
                                           "would be left on disk")))
    from core.data.equities import EquityDataProvider
    rt._eq_provider = EquityDataProvider.__new__(EquityDataProvider)

    out = rt.tick()

    assert out["no_trade"] is True, "a basis change must produce a NO-TRADE tick"
    assert out["rebalanced"] is False, "it must not generate turnover"
    kinds = [i["kind"] for i in rt.state["data_incidents"]]
    assert "NO-TRADE: data provider changed" in kinds, kinds
    # still marking: equity moved on the +1% AAPL print
    assert rt.state["equity_history"]["s4"], "the book must keep marking"


def test_the_same_provider_still_trades_normally(tmp_path, monkeypatch):
    """The guard must not fire on the healthy path, or it becomes the outage."""
    from core.data.equities import EquityDataProvider
    from runtime.live import LiveRuntime

    rt = _runtime(tmp_path, monkeypatch, provider_of_record="yfinance")
    rt._eq_provider = EquityDataProvider.__new__(EquityDataProvider)
    monkeypatch.setattr(LiveRuntime, "_fetch_heavy",
                        lambda self: ({}, {}, rt._pd.Series(dtype=float), False))

    out = rt.tick()
    kinds = [i["kind"] for i in rt.state["data_incidents"]]
    assert "NO-TRADE: data provider changed" not in kinds, (
        "the provider did not change; the guard fired anyway")


def test_the_first_tick_is_not_treated_as_a_change(tmp_path, monkeypatch):
    """Bootstrap has no provider of record. Absent must not read as changed -
    that is the defect class this repo has paid for most."""
    from core.data.equities import EquityDataProvider
    from runtime.live import LiveRuntime

    rt = _runtime(tmp_path, monkeypatch, provider_of_record="")
    rt.state.pop("data_provider", None)
    rt._eq_provider = EquityDataProvider.__new__(EquityDataProvider)
    monkeypatch.setattr(LiveRuntime, "_fetch_heavy",
                        lambda self: ({}, {}, rt._pd.Series(dtype=float), False))

    out = rt.tick()
    kinds = [i["kind"] for i in rt.state["data_incidents"]]
    assert "NO-TRADE: data provider changed" not in kinds, (
        "an absent provider of record was read as a change")
