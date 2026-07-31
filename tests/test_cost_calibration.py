"""Cost-surface calibration (director wish 2026-07-21): realized IBKR fills
vs the modeled CostModel prediction. All offline - the reference-price
fetcher is injected, never the network."""
import pytest

import scripts.cost_calibration as C
from core.broker.costs import CostModel, CostParams
from core.config import CONFIG


def _model():
    return CostModel(CostParams(**CONFIG.costs.equities))


def fill(**kw):
    base = {"id": "e1", "symbol": "AAPL", "side": "BOT", "shares": 100,
            "price": 200.0, "time": "2026-07-21 14:30:01+00:00",
            "commission": 1.0}
    base.update(kw)
    return base


def test_modeled_matches_costmodel_with_stored_inputs():
    rows = C.analyze_fills([fill()], {"AAPL": [200e6, 0.015]},
                           lambda sym, day: 199.0)
    assert len(rows) == 1
    r = rows[0]
    bd = _model().estimate(100 * 200.0, adv=200e6, daily_vol=0.015)
    assert r["modeled_slippage_bps"] == pytest.approx(bd.slippage * 1e4, abs=1e-3)
    assert r["modeled_total_bps"] == pytest.approx(bd.total * 1e4, abs=1e-3)
    # buy filled above the decision price -> positive realized slippage
    assert r["realized_slippage_bps"] == pytest.approx(
        (200.0 - 199.0) / 199.0 * 1e4, abs=1e-3)
    assert r["realized_commission_bps"] == pytest.approx(
        1.0 / 20_000.0 * 1e4, abs=1e-3)
    assert r["realized_total_bps"] == pytest.approx(
        r["realized_slippage_bps"] + r["realized_commission_bps"], abs=1e-3)


def test_sell_slippage_sign_and_price_improvement():
    # sold below the decision price -> cost paid (positive)
    worse = C.analyze_fills([fill(id="a", side="SLD", price=198.0)],
                            {}, lambda s, d: 200.0)[0]
    assert worse["realized_slippage_bps"] == pytest.approx(100.0, abs=1e-6)
    # sold above it -> price improvement (negative)
    better = C.analyze_fills([fill(id="b", side="SLD", price=202.0)],
                             {}, lambda s, d: 200.0)[0]
    assert better["realized_slippage_bps"] < 0


def test_missing_inputs_use_pessimistic_fallbacks():
    # a name with no stamped cost inputs models against the SAME fallbacks
    # the live marking loop charges (ADV floor + typical vol)
    r = C.analyze_fills([fill(symbol="ZZZZ")], {}, lambda s, d: None)[0]
    bd = _model().estimate(20_000.0, adv=C.FALLBACK_ADV,
                           daily_vol=C.FALLBACK_DVOL)
    assert r["modeled_total_bps"] == pytest.approx(bd.total * 1e4, abs=1e-3)


def test_dedup_and_invalid_fills_skipped():
    rows = C.analyze_fills(
        [fill(), fill(),                          # duplicate execId
         fill(id="e2", shares=0),                 # zero shares
         fill(id="e3", price=0.0)],               # zero price
        {}, lambda s, d: None)
    assert len(rows) == 1


def test_no_reference_price_slippage_is_none():
    r = C.analyze_fills([fill()], {}, lambda s, d: None)[0]
    assert r["realized_slippage_bps"] is None
    assert r["realized_total_bps"] is None
    assert r["realized_commission_bps"] is not None   # still calibrated


def test_ref_fn_exceptions_never_raise():
    def boom(s, d):
        raise RuntimeError("network down")
    r = C.analyze_fills([fill()], {}, boom)[0]
    assert r["realized_slippage_bps"] is None


def test_summary_bound_fraction_buckets_and_worst():
    fills = [fill(id="x1", price=200.0),                              # clean
             fill(id="x2", symbol="MSFT", shares=100, price=210.0)]  # ugly
    # AAPL fills exactly at decision price (slip 0, comm 0.5bps < modeled);
    # MSFT fills 5% through it (realized >> modeled) -> bound fraction 0.5
    rows = C.analyze_fills(fills, {"AAPL": [200e6, 0.015],
                                   "MSFT": [200e6, 0.015]},
                           lambda s, d: 200.0)
    summ = C.summarize(rows)
    assert summ["n_fills"] == 2 and summ["n_with_slippage"] == 2
    assert summ["frac_modeled_bounds_realized"] == pytest.approx(0.5)
    assert "$10k-$50k" in summ["buckets"]
    assert summ["buckets"]["$10k-$50k"]["n"] == 2
    assert summ["worst_fills"][0]["symbol"] == "MSFT"
    # the report renders without a live state file
    txt = C.format_report(summ)
    assert "COST-SURFACE CALIBRATION" in txt and "MSFT" in txt


def test_bucket_edges():
    assert C.bucket_of(0.0) == "<$2k"
    assert C.bucket_of(1_999.99) == "<$2k"
    assert C.bucket_of(2_000.0) == "$2k-$10k"
    assert C.bucket_of(75_000.0) == ">$50k"


# ------------------------------------------------- clean-execution filter --
# Every mirror session through 2026-07-29 executed under E37 order stacking
# and/or the E39/E40 stale-plan overnight reversal (97% round-trip at 21x NAV
# on 07-27). Calibrating impact_coef on that self-inflicted churn would fit the
# cost model to a bug, so CLEAN_START is frozen at the first verified-clean
# session and any LATER session mirror_reconcile flags CHURN - a mirror
# regression - drops out of the dataset automatically.

def test_clean_start_is_frozen_after_both_mirror_fixes():
    assert C.CLEAN_START == "2026-07-30"


def test_filter_excludes_pre_clean_start_sessions():
    fills = [fill(id="a1", time="2026-07-27 14:31:02+00:00"),
             fill(id="a2", time="2026-07-27 20:59:00+00:00"),
             fill(id="a3", time="2026-07-28 15:02:00+00:00"),
             fill(id="k1", time="2026-07-30 15:00:01+00:00"),
             fill(id="k2", symbol="MSFT", time="2026-07-30 15:30:00+00:00")]
    kept, rep = C.filter_calibration_fills(fills, {})
    assert [f["id"] for f in kept] == ["k1", "k2"]
    assert rep["n_input"] == 5 and rep["n_kept"] == 2
    assert rep["n_pre_clean"] == 3
    assert rep["pre_clean_days"] == ["2026-07-27", "2026-07-28"]
    assert rep["n_undated"] == 0
    assert rep["n_churn_flagged"] == 0 and rep["churn_days"] == []


def test_post_clean_start_churn_session_is_excluded():
    # 2026-08-03: ONE symbol round-tripped five times - gross ~$3.0M, net ~0,
    # far past mirror_reconcile's CHURN_FRAC_BAR / CHURN_GROSS_FLOOR. That is a
    # mirror REGRESSION, not market impact, so the day leaves the dataset.
    churny = [fill(id=f"c{i}", symbol="SPY",
                   side="BOT" if i % 2 == 0 else "SLD",
                   shares=2000, price=150.0,
                   time=f"2026-08-03 15:{i:02d}:00+00:00")
              for i in range(10)]
    clean = [fill(id=f"n{i}", symbol=sym, side="BOT", shares=10, price=150.0,
                  time=f"2026-08-04 15:{i:02d}:00+00:00")
             for i, sym in enumerate(("AAPL", "MSFT", "NVDA"))]
    state = {"ibkr": {"nav": 5000.0, "net_liquidation": 5000.0, "fills": []}}

    days = C._churn_days(churny + clean, state)
    assert days is not None, "churn layer must be available in-repo"
    assert "2026-08-03" in days and "2026-08-04" not in days

    kept, rep = C.filter_calibration_fills(churny + clean, state)
    assert [f["id"] for f in kept] == ["n0", "n1", "n2"]
    assert rep["n_churn_flagged"] == 10
    assert rep["churn_days"] == ["2026-08-03"]
    assert rep["n_pre_clean"] == 0 and rep["churn_layer"] != "unavailable"


def test_undated_fills_are_excluded_and_counted():
    # an undatable fill cannot be certified clean -> it is not calibration data
    fills = [fill(id="u1", time=None),
             fill(id="u2", time="not-a-date-at-all"),
             fill(id="k1", time="2026-07-31 15:00:01+00:00")]
    kept, rep = C.filter_calibration_fills(fills, {})
    assert [f["id"] for f in kept] == ["k1"]
    assert rep["n_undated"] == 2
    assert rep["n_pre_clean"] == 0 and rep["n_kept"] == 1


def test_include_churn_keeps_everything_deduped():
    # forensics override: filters nothing, still dedupes by execId
    fills = [fill(id="a1", time="2026-07-27 14:31:02+00:00"),
             fill(id="a1", time="2026-07-27 14:31:02+00:00"),   # dup execId
             fill(id="b1", time="2026-07-30 15:00:01+00:00"),
             fill(id="u1", time=None)]
    kept, rep = C.filter_calibration_fills(fills, {}, include_churn=True)
    assert [f["id"] for f in kept] == ["a1", "b1", "u1"]
    assert rep["n_input"] == 4 and rep["n_kept"] == 3
    assert rep["n_pre_clean"] == 0 and rep["pre_clean_days"] == []
    assert rep["n_churn_flagged"] == 0 and rep["churn_days"] == []
    assert rep["n_undated"] == 0 and rep["include_churn"] is True


def test_duplicate_exec_ids_collapse_before_counting():
    keep = fill(id="d1", time="2026-07-30 15:00:01+00:00")
    pre = fill(id="p1", time="2026-07-27 14:31:00+00:00")
    fills = [keep, dict(keep),                     # ledger + ring re-admit
             fill(id="d2", symbol="MSFT", time="2026-07-30 15:05:00+00:00"),
             pre, dict(pre)]
    kept, rep = C.filter_calibration_fills(fills, {})
    assert [f["id"] for f in kept] == ["d1", "d2"]
    assert rep["n_input"] == 5 and rep["n_kept"] == 2
    assert rep["n_pre_clean"] == 1                 # duplicate counted once
    assert rep["pre_clean_days"] == ["2026-07-27"]


def test_churn_layer_unavailable_still_applies_date_cutoff(monkeypatch):
    monkeypatch.setattr(C, "_churn_days", lambda fills, state: None)
    fills = [fill(id="a1", time="2026-07-28 15:00:00+00:00"),
             fill(id="k1", time="2026-07-31 15:00:01+00:00")]
    kept, rep = C.filter_calibration_fills(fills, {})
    assert [f["id"] for f in kept] == ["k1"]
    assert rep["churn_layer"] == "unavailable"
    assert rep["n_pre_clean"] == 1
    assert rep["n_churn_flagged"] == 0 and rep["churn_days"] == []


def test_churn_helper_never_raises_on_junk():
    out = C._churn_days([None, "not a fill", {"time": "garbage"}], {})
    assert out is None or isinstance(out, set)


def test_filter_never_raises_on_malformed_rows():
    fills = [None, "junk", {}, {"id": "", "time": "2026-07-30"},
             fill(id="k1", time="2026-07-30 15:00:01+00:00")]
    kept, rep = C.filter_calibration_fills(fills, {})
    assert [f["id"] for f in kept] == ["k1"]
    assert rep["n_input"] == 5 and rep["n_kept"] == 1
    assert rep["n_no_id"] == 4


def test_main_passes_only_kept_fills_and_warns_on_regression(
        monkeypatch, tmp_path, capsys):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"cost_inputs": {}}')
    rows = [fill(id="c1", time="2026-08-03 15:00:00+00:00"),
            fill(id="k1", time="2026-08-04 15:00:00+00:00")]
    seen: dict = {}

    def fake_analyze(fills, cost_inputs, ref_fn):
        seen["ids"] = [f["id"] for f in fills]
        return []

    monkeypatch.setattr(C, "load_fills", lambda state: list(rows))
    monkeypatch.setattr(C, "_churn_days", lambda fills, state: {"2026-08-03"})
    monkeypatch.setattr(C, "make_prior_close_fn",
                        lambda *a, **k: (lambda s, d: None))
    monkeypatch.setattr(C, "analyze_fills", fake_analyze)
    monkeypatch.setattr("sys.argv",
                        ["cost_calibration.py", "--state", str(state_path)])
    C.main()
    out = capsys.readouterr().out
    assert seen["ids"] == ["k1"]                   # churn day never calibrated
    assert "calibration set: kept 1 of 2 fills" in out
    assert "WARNING: post-CLEAN_START CHURN session(s)" in out
    assert "2026-08-03" in out


def test_main_reports_when_no_clean_fills_exist(monkeypatch, tmp_path, capsys):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"cost_inputs": {}}')

    def never(*a, **k):
        raise AssertionError("defect-era fills must never reach analyze_fills")

    monkeypatch.setattr(C, "load_fills", lambda state: [
        fill(id="a1", time="2026-07-27 14:31:00+00:00"),
        fill(id="a2", time="2026-07-29 18:00:00+00:00")])
    monkeypatch.setattr(C, "analyze_fills", never)
    monkeypatch.setattr("sys.argv",
                        ["cost_calibration.py", "--state", str(state_path)])
    C.main()
    out = capsys.readouterr().out
    assert "kept 0 of 2 fills" in out
    assert "no clean fills yet" in out and "2026-07-30" in out


def test_main_include_churn_flag_restores_defect_era_fills(
        monkeypatch, tmp_path, capsys):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"cost_inputs": {}}')
    seen: dict = {}

    def fake_analyze(fills, cost_inputs, ref_fn):
        seen["ids"] = [f["id"] for f in fills]
        return []

    monkeypatch.setattr(C, "load_fills", lambda state: [
        fill(id="a1", time="2026-07-27 14:31:00+00:00"),
        fill(id="k1", time="2026-07-30 15:00:00+00:00")])
    monkeypatch.setattr(C, "make_prior_close_fn",
                        lambda *a, **k: (lambda s, d: None))
    monkeypatch.setattr(C, "analyze_fills", fake_analyze)
    monkeypatch.setattr("sys.argv", ["cost_calibration.py", "--state",
                                     str(state_path), "--include-churn"])
    C.main()
    out = capsys.readouterr().out
    assert seen["ids"] == ["a1", "k1"]
    assert "calibration set: kept 2 of 2 fills" in out
