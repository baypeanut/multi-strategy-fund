"""The instruments state.json carries and nobody read (2026-08-05).

realized_vol / s5_stats / data_provider were written every tick and surfaced to
no agent-facing view; these pin them as additive, null-safe passthroughs.
"""
import json

import research.engineer as eng


def _payload(state):
    view = eng._quant_view(state)
    return json.loads(view.partition("\n")[2])


def test_realized_vol_reaches_the_nightly_view():
    rv = {"s1": {"realized_annual": 0.0827, "target_annual": 0.10,
                 "ratio": 0.827, "n": 42},
          "s3": {"realized_annual": 0.086, "target_annual": 0.10,
                 "ratio": 0.86, "n": 42}}
    p = _payload({"realized_vol": rv})
    assert p["realized_vol_vs_target"] == rv


def test_s5_stats_and_provider_reach_the_view():
    p = _payload({"s5_stats": {"n_units": 9, "n_names": 7, "n_events": 30},
                  "data_provider": "polygon"})
    assert p["s5_stats"]["n_units"] == 9
    assert p["data_provider"] == "polygon"


def test_absent_keys_render_as_null_never_raise():
    p = _payload({})
    assert p["realized_vol_vs_target"] is None
    assert p["s5_stats"] is None
    assert p["data_provider"] is None


def test_the_addition_is_additive():
    p = _payload({"equity_history": {"s1": [["t0", 100.0], ["t1", 101.0]]},
                  "s3_vs_s1": {"n": 14}})
    assert p["books_since_inception"]["s1"]["total_ret_pct"] == 1.0
    assert p["s3_vs_s1_PREREGISTERED_RULE"]["n"] == 14
