"""W3 tests: common vol normalization + paired (DM/Newey-West) test."""
import numpy as np
import pandas as pd

from backtest.metrics import paired_test
from systems.s1_quant.covariance import ledoit_wolf_cov, portfolio_vol
from systems.s1_quant.portfolio import scale_to_target_vol

rng = np.random.default_rng(3)


# --- scale_to_target_vol ---------------------------------------------------
def make_cov(n=10, daily_vol=0.02):
    data = pd.DataFrame(rng.normal(0, daily_vol, (500, n)),
                        columns=[f"S{i}" for i in range(n)])
    return ledoit_wolf_cov(data)


def test_scaling_hits_target_when_caps_loose():
    cov = make_cov()
    w = pd.Series(rng.normal(0, 1, 10), index=cov.index)
    out = scale_to_target_vol(w, cov, target_vol=0.10, max_position=1.0, max_gross=10.0)
    assert abs(portfolio_vol(out, cov) - 0.10) < 1e-6


def test_scaling_respects_caps():
    cov = make_cov()
    w = pd.Series(rng.normal(0, 1, 10), index=cov.index)
    out = scale_to_target_vol(w, cov, target_vol=0.50, max_position=0.05, max_gross=1.0)
    assert out.abs().max() <= 0.05 + 1e-12
    assert out.abs().sum() <= 1.0 + 1e-12


def test_zero_weights_pass_through():
    cov = make_cov()
    w = pd.Series(0.0, index=cov.index)
    out = scale_to_target_vol(w, cov, target_vol=0.10)
    assert out.abs().sum() == 0.0


def test_regime_scales_target_not_shape():
    # halving target vol should halve every weight (caps loose), not reshape
    cov = make_cov()
    w = pd.Series(rng.normal(0, 1, 10), index=cov.index)
    full = scale_to_target_vol(w, cov, 0.10, max_position=1.0, max_gross=10.0)
    half = scale_to_target_vol(w, cov, 0.05, max_position=1.0, max_gross=10.0)
    ratio = (half / full).dropna()
    assert np.allclose(ratio, 0.5, atol=1e-9)


# --- paired test -------------------------------------------------------------
def test_paired_equal_series_not_significant():
    base = pd.Series(rng.normal(5e-4, 0.01, 500))
    noise_a = base + rng.normal(0, 5e-4, 500)
    noise_b = base + rng.normal(0, 5e-4, 500)
    res = paired_test(noise_a, noise_b)
    assert res["p_value"] > 0.05
    assert abs(res["mean_daily_bps"]) < 5


def test_paired_detects_real_edge_in_correlated_books():
    # two highly correlated books, A has +2bps/day true edge; each book adds
    # 10bps idiosyncratic noise so diff sigma = sqrt(2)*1e-3 ~ 1.41e-3.
    # t = 2e-4/(1.41e-3/sqrt(n)) = 0.142*sqrt(n) -> ~2.8 at n=400
    n = 400
    base = pd.Series(rng.normal(5e-4, 0.01, n))
    a = base + 2e-4 + rng.normal(0, 1e-3, n)
    b = base + rng.normal(0, 1e-3, n)
    res = paired_test(a, b)
    assert res["p_value"] < 0.05
    assert res["mean_daily_bps"] > 0


def test_paired_small_n_returns_nan():
    a = pd.Series([0.01] * 5)
    b = pd.Series([0.00] * 5)
    res = paired_test(a, b)
    assert np.isnan(res["p_value"]) and res["n"] == 5


def test_paired_autocorrelation_does_not_blow_up():
    # AR(1) differences: NW must widen SE vs naive, not crash
    n = 400
    e = np.zeros(n)
    for t in range(1, n):
        e[t] = 0.5 * e[t - 1] + rng.normal(0, 1e-3)
    a = pd.Series(rng.normal(5e-4, 0.01, n))
    res = paired_test(a + e, a)
    assert np.isfinite(res["t_stat"])


# --- W3 wiring: the runtime must APPLY the shared normaliser ------------------
def test_every_book_is_routed_through_the_one_vol_normaliser():
    """E63c. Every test above proves scale_to_target_vol WORKS. None proved the
    runtime USES it, and three of the four books did not have to.

    Measured 2026-08-11 on the production tree, bypassing the normaliser one
    book at a time by replacing the call with an identity lambda:

        S1 vol-target bypassed          594 passed  (missed)
        S3 vol-target bypassed          594 passed  (missed)
        S4 vol-target bypassed          594 passed  (missed)
        S4 target x3, others unchanged  594 passed  (missed)
        S5 vol-target bypassed          1 failed    (caught)

    W3 is the invariant the registered question rests on: "All five books share
    one ex-ante vol normalization (10% target via scale_to_target_vol)". If S1
    and S3 are normalised to different risk, the paired DM test measures which
    book took more risk rather than which book has more skill, and the 60-day
    verdict means nothing.

    The fund's own instrument already suspected it. _realized_vol reports S3
    realising 8.60% against a 7.54% target while S1 realised 8.27% against 10%,
    and concludes: "The race is currently fair by accident rather than by
    construction, and nobody could have known either way."

    HONEST LIMITATION, stated rather than dressed up. This is a STRUCTURAL pin:
    it reads the source of _run_systems. That is the weaker kind, and this repo
    has already been bitten by it - E59b records a frozen test that pinned the
    string "reg.risk_scale" inside this very function while the value was
    reached by another route. It catches the threat it is aimed at (a call
    removed, or a book given a different target) and it verifiably does: all
    four mutations above turn it red. It CANNOT catch a change that keeps the
    call and alters its effect, and no test here does. A behavioural pin would
    need to drive _run_systems end to end, which every existing test stubs out
    because the S2 path reaches the network; building that fixture is worth
    doing and is filed rather than faked."""
    import inspect

    import runtime.live as live_mod
    from core.config import CONFIG

    src = inspect.getsource(live_mod.LiveRuntime._run_systems)
    routed = src.count("scale_to_target_vol(")
    assert routed >= 3, (
        f"_run_systems routes only {routed} books through the shared vol "
        f"normaliser; S1, S3 and S4 must each go through it (W3)")

    targets = {line.split("target_vol=")[1].split(",")[0].strip()
               for line in src.splitlines() if "target_vol=" in line}
    assert targets == {"CONFIG.risk.vol_target_annual"}, (
        f"books are normalised to different targets: {targets}. The horse race "
        f"is fair only while every book shares one ex-ante vol target.")
    assert CONFIG["risk"]["vol_target_annual"] == 0.10


def test_a_superiority_claim_cannot_be_read_without_its_risk_ratio(tmp_path):
    """E63d. The registered rule gates on n and p. It does not gate on whether
    the two books ran at comparable RISK, and on 2026-08-12 they did not: S3
    realised 13.45% annualised against S1's 9.94%, a ratio of 1.35, while
    beating S1 by 4.34pp over the same window.

    Ex-ante fairness holds and is the registered property - both books target
    10% and both hit it. The gap is estimation error: S3 holds ~40 names carved
    from a 500-name shrunk covariance so its vol is underestimated, S1 holds 500
    and the errors average out.

    Sizing and the decision rule are deliberately untouched; changing either
    resets the clock and relitigates a pre-registration. Instead the ratio
    travels WITH the verdict, so nobody can write "S3 beat S1" at day 60 without
    the number that says whether it was skill or leverage."""
    import pandas as pd

    from runtime.live import LiveRuntime

    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))
    days = pd.bdate_range("2026-01-05", periods=40)
    # S3 is deliberately the hotter book: same drift, 1.5x the swing
    e1, e3, h1, h3 = 100.0, 100.0, [], []
    for i, d in enumerate(days):
        if i:
            step = 0.01 if i % 2 else -0.008
            e1 *= 1 + step
            e3 *= 1 + step * 1.5
        h1.append([d.isoformat(), e1])
        h3.append([d.isoformat(), e3])
    rt.state["equity_history"]["s1"] = h1
    rt.state["equity_history"]["s3"] = h3
    rt.state.pop("clock_start", None)

    out = rt._paired_s3_vs_s1()

    assert "vol_ratio_s3_s1" in out, (
        "the paired readout must carry the realised-vol ratio, or a raw-return "
        "win by a hotter book reads as a skill claim")
    assert out["vol_ratio_s3_s1"] > 1.3, out["vol_ratio_s3_s1"]
    assert out["risk_comparable"] is False, (
        "a book running 1.5x the risk of its control must not be labelled "
        "risk-comparable")


def test_equal_risk_books_are_labelled_comparable(tmp_path):
    """The other side: the label must not fire on a fair race, or it becomes
    noise and gets ignored like every other alarm in this repo."""
    import pandas as pd

    from runtime.live import LiveRuntime

    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))
    days = pd.bdate_range("2026-01-05", periods=40)
    e1, e3, h1, h3 = 100.0, 100.0, [], []
    for i, d in enumerate(days):
        if i:
            step = 0.01 if i % 2 else -0.008
            e1 *= 1 + step
            e3 *= 1 + step * 1.02          # same risk, a hair of drift
        h1.append([d.isoformat(), e1])
        h3.append([d.isoformat(), e3])
    rt.state["equity_history"]["s1"] = h1
    rt.state["equity_history"]["s3"] = h3
    rt.state.pop("clock_start", None)

    out = rt._paired_s3_vs_s1()
    assert out["risk_comparable"] is True, out.get("vol_ratio_s3_s1")


def test_the_per_name_cap_does_not_destroy_market_neutrality():
    """E64. build_weights promises market-neutrality in step 2 and then clips in
    step 4, and nothing used to restore it.

    Measured on the live book 2026-08-18: the S1 equity sleeve left the engine at
    net -0.0555 on gross 0.2038 - 27% of gross net SHORT - with
    market_neutral=True. The decisive test was loosening the cap:

        max_position 0.05  ->  net -0.0555  (-27.3% of gross)   LIVE
        max_position 0.10  ->  net -0.0055  ( -2.2%)
        max_position 0.25  ->  net -0.0000  (  0.0%)
        max_position inf   ->  net -0.0000  (  0.0%)

    Net going to exactly zero as the cap loosens is the proof that the clip, not
    the signals, was the cause. And the caller then scales the book UP to reach
    the vol target, so the imbalance was magnified: -0.044 became -0.255 on the
    live merged book. S1 is the CONTROL arm of the registered experiment, so it
    was being compared to S3 while carrying a 23% net short nobody chose, in a
    market that rose 3.74% over the same window, at a beta of -0.14.

    A skewed signal distribution is the trigger: a few names score far above the
    rest, get clipped at the cap, and lose more weight than the diffuse other
    side. The fixture below reproduces exactly that shape."""
    import numpy as np
    import pandas as pd

    from systems.s1_quant.portfolio import construct_signal_portfolio

    n = 60
    idx = [f"S{i}" for i in range(n)]
    # heavy right tail: 5 names far out, the rest mildly negative. Demeaning
    # centres this, then a tight cap bites the 5 and leaves the book net short.
    scores = pd.Series([8.0, 7.0, 6.5, 6.0, 5.5] + [-0.2] * (n - 5),
                       index=idx, dtype=float)
    vols = pd.Series(0.2, index=idx)
    cov = pd.DataFrame(np.eye(n) * (0.2 ** 2 / 252), index=idx, columns=idx)

    w = construct_signal_portfolio(scores, cov, vols, target_vol=0.10,
                      max_position=0.05, max_gross=1.00, market_neutral=True)

    gross = w.abs().sum()
    assert gross > 0
    assert abs(w.sum()) < 0.01 * gross, (
        f"market_neutral=True produced net {w.sum():+.4f} on gross {gross:.4f} "
        f"({w.sum() / gross:+.1%} of gross). The per-name cap has undone the "
        f"demeaning and nothing restored it.")
    assert w.abs().max() <= 0.05 + 1e-9, (
        "re-neutralising must not push a name back over the per-name cap")


def test_market_neutral_false_is_still_allowed_to_be_directional():
    """The crypto sleeve is built market_neutral=False on purpose - eight names
    cannot be cross-sectionally neutralised meaningfully. The fix must not
    quietly neutralise a book that asked not to be."""
    import numpy as np
    import pandas as pd

    from systems.s1_quant.portfolio import construct_signal_portfolio

    idx = ["A", "B", "C", "D"]
    scores = pd.Series([3.0, 2.0, 1.5, 1.0], index=idx)      # all positive
    vols = pd.Series(0.5, index=idx)
    cov = pd.DataFrame(np.eye(4) * (0.5 ** 2 / 252), index=idx, columns=idx)

    w = construct_signal_portfolio(scores, cov, vols, target_vol=0.10, max_position=0.50,
                      max_gross=1.00, market_neutral=False)
    assert w.sum() > 0.01, "a directional book was neutralised against its will"


def test_the_vol_ratio_is_suppressed_until_it_can_be_estimated(tmp_path):
    """E69. The realised-vol ratio I added (E63d) emitted at >=5 daily points,
    but a 5-6 point std is not a vol estimate. On the live book at n=6 it printed
    11.9 - S1's freshly-neutral control barely moved over a few low-VIX days
    (ann vol 1.0%) while S3 ran 12.0% - a number a reader could take for a real
    risk gap. _realized_vol itself requires >=10 points; the ratio now honours
    the same floor and reports nothing below it, because a vol you cannot
    estimate is absent, not a number."""
    import pandas as pd

    from runtime.live import LiveRuntime

    rt = LiveRuntime(state_path=str(tmp_path / "state.json"))
    days = pd.bdate_range("2026-01-05", periods=8)          # 8 closes -> 7 returns, < 10
    e1 = e3 = 100.0
    h1, h3 = [], []
    for i, dte in enumerate(days):
        if i:
            e1 *= 1.0005
            e3 *= 1.0005 + (0.004 if i % 2 else -0.003)
        h1.append([dte.isoformat(), e1]); h3.append([dte.isoformat(), e3])
    rt.state["equity_history"]["s1"] = h1
    rt.state["equity_history"]["s3"] = h3
    rt.state.pop("clock_start", None)

    out = rt._paired_s3_vs_s1()
    assert out.get("vol_ratio_s3_s1") is None, (
        f"ratio emitted at n={out.get('n')} (<10): {out.get('vol_ratio_s3_s1')}; "
        f"a std from fewer than 10 points is not a vol estimate")
    assert out.get("risk_comparable") is None
