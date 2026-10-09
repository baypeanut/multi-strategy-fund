#!/usr/bin/env python3
"""Run actual model proposal/critique calls using explicitly synthetic evidence.

This demonstration verifies structured model research and deterministic gates,
not trading profitability. It does not submit orders or mutate fund state.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

# Allow direct `python scripts/demo_model_research.py` without PYTHONPATH changes.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm import GatewayClient, VLLMError

SYNTHETIC_FIXTURE = {
    "label": "SYNTHETIC_PIPELINE_FIXTURE_NOT_MARKET_DATA",
    "is_synthetic": True,
    "is_out_of_sample": False,
    "leakage_audit_complete": False,
    "gross_trade_returns_bps": [20, -15, 40, -30, 35, -20, 15, -25, 30, -40, 25, -10],
    "round_trip_cost_bps": 10,
    "return_basis": "sum of individual equal-notional trade returns; not compounded portfolio NAV",
}
PROPOSAL_PROMPT = (
    "RESEARCH DEMONSTRATION ONLY. Propose one falsifiable intraday stock research "
    "experiment using an opening-range breakout. Do not imply it is profitable "
    "or deployable. No market data or backtest is available. Use exactly three "
    "brief reproducible rules and three brief risks. Define a conservative next-bar "
    "entry after a confirmed signal, explicit exit and transaction-cost treatment. "
    "The verdict concerns whether to test the hypothesis only; no order or live "
    "fund permission may be given. Keep the entire response under 300 words."
)
SYNTHETIC_PORTFOLIO_BRIEFING = {
    "label": "SYNTHETIC_BRIEFING_NOT_MARKET_DATA",
    "is_synthetic": True,
    "allowed_symbols": ["SPY", "QQQ"],
    "nav": 10000,
    "names": [{"symbol": "SPY", "s1_quant_z": 1.0, "s2_news_z": 0.3},
              {"symbol": "QQQ", "s1_quant_z": -1.0, "s2_news_z": -0.3}],
    "warning": "Fabricated input for interface/risk-gate demonstration. Never trade it.",
}


def evaluate_synthetic_fixture(fixture: dict) -> dict:
    """Plain deterministic arithmetic, explicitly not an empirical backtest."""
    gross = fixture["gross_trade_returns_bps"]
    cost = fixture["round_trip_cost_bps"]
    if not isinstance(gross, list) or not gross:
        raise ValueError("fixture requires trade returns")
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in gross):
        raise ValueError("fixture returns must be finite")
    if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
        raise ValueError("fixture cost must be finite and nonnegative")
    net = [v - cost for v in gross]
    result = {
        "sample_trades": len(gross), "gross_trade_return_sum_bps": sum(gross),
        "modeled_cost_sum_bps": len(gross) * cost,
        "net_trade_return_sum_bps": sum(net),
        "net_trade_returns_bps": net,
        "net_winners": sum(v > 0 for v in net),
        "net_losers": sum(v < 0 for v in net),
        "breakeven_trades": sum(v == 0 for v in net),
        "fixture_only": True,
        "is_real_backtest": False,
        "return_basis": fixture["return_basis"],
    }
    reasons = []
    if fixture.get("is_synthetic") is not False:
        reasons.append("synthetic inputs are not evidence of market performance")
    if fixture.get("is_out_of_sample") is not True:
        reasons.append("no independently held-out market evaluation")
    if fixture.get("leakage_audit_complete") is not True:
        reasons.append("no completed leakage/causality audit")
    if len(gross) < 30:
        reasons.append("insufficient sample: fewer than 30 fixture trades")
    if sum(net) <= 0:
        reasons.append("nonpositive net trade-return sum after modeled costs")
    # This demonstration cannot authorize deployment even with a favorable fixture.
    result["deterministic_gate"] = {
        "eligible_for_live_trading": False,
        "decision": "research_only_no_orders",
        "reasons": reasons + ["demo has no broker or deployment authorization interface"],
        "llm_verdict_can_override_gate": False,
    }
    return result


def _call(client: GatewayClient, role: str, task: str, prompt: str,
          max_tokens: int, symbols: list[str] | None = None) -> dict:
    started = time.perf_counter()
    result = client.generate(task=task, prompt=prompt, symbols=symbols,
                             max_tokens=max_tokens, temperature=0.0)
    return {
        "role": role, "task": task,
        "actual_http_generation": isinstance(client, GatewayClient),
        "transport": "validated_gateway_http" if isinstance(client, GatewayClient) else "test-double",
        "model": result.model, "request_id": result.request_id,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "client_latency_seconds": time.perf_counter() - started,
        "usage": {"input_tokens": result.input_tokens, "output_tokens": result.output_tokens},
        "data": result.data,
    }


def read_model_info(client: GatewayClient) -> dict:
    body = client.model_info()
    if (not isinstance(body, dict) or body.get("model") != client.model
            or body.get("backend") != "vllm"
            or body.get("deployment_environment") == "test-double"):
        raise ValueError("demo requires a matching non-test-double vLLM backend declaration")
    return {k: body[k] for k in ("model", "backend", "deployment_environment", "model_revision", "vllm_version")
            if isinstance(body.get(k), str)}


def run_demo(client: GatewayClient, model_info: dict, include_portfolio: bool = False,
             max_tokens: int = 512) -> dict:
    evaluated = evaluate_synthetic_fixture(SYNTHETIC_FIXTURE)
    proposal = _call(client, "research_proposer", "research", PROPOSAL_PROMPT, max_tokens)
    critique_prompt = (
        "Critique the following model-proposed hypothesis using ONLY the supplied "
        "SYNTHETIC PIPELINE FIXTURE. The figures are fabricated for a software demo, "
        "not market data, not a strategy backtest, and not out-of-sample evidence. "
        "Identify transaction costs, insufficient sample, and missing validation. "
        "Use exactly three brief rules for the next research step and three brief "
        "risks. Reject any claim of proven profitability or live-trading readiness. "
        "Do not fabricate additional measurements. Keep under 300 words.\n" +
        json.dumps({"proposed_hypothesis": proposal["data"], "fixture": SYNTHETIC_FIXTURE,
                    "deterministic_evaluation": evaluated}, allow_nan=False)
    )
    critic = _call(client, "research_critic", "research", critique_prompt, max_tokens)
    roles = [proposal, critic]
    risk_demo = None
    if include_portfolio:
        from systems.s3_llm.wrapper import RiskWrapper
        prompt = (
            "This is a SYNTHETIC interface demonstration, not current trading evidence. "
            "Propose modest opposite paper weights from the fabricated scores, at most "
            "5% absolute per symbol. An empty proposal is allowed. Do not submit orders. "
            "A deterministic wrapper will review all weights.\n" +
            json.dumps(SYNTHETIC_PORTFOLIO_BRIEFING, allow_nan=False)
        )
        portfolio = _call(client, "paper_portfolio_proposer", "portfolio", prompt, min(max_tokens, 256), ["SPY", "QQQ"])
        roles.append(portfolio)
        raw = {position["symbol"]: position["weight"] for position in portfolio["data"]["positions"]}
        wrapper = RiskWrapper(universe={"SPY", "QQQ"}, max_position=0.01,
                              max_gross=0.015, adv_cap=0.01, max_turnover=0.01, nav=10000)
        constrained = wrapper.validate(raw, adv={"SPY": 10000, "QQQ": 5000}, current_weights={})
        weights = {str(k): float(v) for k, v in constrained.weights.items()}
        risk_demo = {
            "input_label": "SYNTHETIC_BRIEFING_NOT_MARKET_DATA",
            "raw_model_proposal": raw, "sanitized_weights": weights,
            "violations": constrained.violations,
            "limits": {"max_position": wrapper.max_position, "max_gross": wrapper.max_gross,
                       "max_turnover": wrapper.max_turnover, "nav": wrapper.nav,
                       "adv_cap": wrapper.adv_cap},
            "synthetic_adv_dollars": {"SPY": 10000, "QQQ": 5000},
            "post_gate_checks": {
                "universe": set(weights) <= wrapper.universe,
                "position": all(abs(w) <= wrapper.max_position + 1e-12 for w in weights.values()),
                "liquidity": all(abs(w) <= wrapper.adv_cap * {"SPY": 10000, "QQQ": 5000}[s] / wrapper.nav + 1e-12
                                  for s, w in weights.items()),
                "gross": sum(abs(w) for w in weights.values()) <= wrapper.max_gross + 1e-12,
                "turnover_from_cash": sum(abs(w) for w in weights.values()) <= wrapper.max_turnover + 1e-12,
            },
            "orders_submitted": 0, "live_trading_eligible": False,
        }
        if not all(risk_demo["post_gate_checks"].values()):
            raise ValueError("deterministic risk demo failed postconditions")
    return {
        "schema_version": 1, "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "Observed structured model research/critique HTTP calls; deterministic synthetic-input gates; no trading performance claim.",
        "provenance": {"endpoint": client.base_url, "model_info_declarations": model_info,
                       "backend_claim": model_info.get("backend"),
                       "evidence_kind": "MODEL_API_WORKLOAD" if isinstance(client, GatewayClient) else "TEST_DOUBLE_CONTRACT",
                       "gpu_execution_verified_by_demo": False,
                       "hardware_evidence_requirement": "Separate observed Kubernetes/vLLM/GPU deployment artifacts are required.",
                       "synthetic_inputs": True, "market_backtest_performed": False,
                       "orders_submitted": 0, "ledger_writes": 0},
        "fixture": SYNTHETIC_FIXTURE, "deterministic_evaluation": evaluated,
        "roles": roles, "risk_wrapper_demo": risk_demo,
        "total_reported_input_tokens": sum(r["usage"]["input_tokens"] for r in roles),
        "total_reported_output_tokens": sum(r["usage"]["output_tokens"] for r in roles),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8081/v1")
    parser.add_argument("--model", default="fund-llm")
    parser.add_argument("--api-key-env", default="MODEL_API_KEY")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--include-portfolio", action="store_true")
    args = parser.parse_args(argv)
    output = Path(__file__).resolve().parent.parent / "research/model_serving_20261009/research_demo.json"
    try:
        if output.exists():
            raise FileExistsError("research demo evidence already exists")
        key = os.environ.get(args.api_key_env, "")
        if not key.strip() or "\n" in key or "\r" in key:
            raise ValueError("missing or invalid gateway key environment")
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 300:
            raise ValueError("timeout outside allowed bounds")
        if not 1 <= args.max_tokens <= 1024:
            raise ValueError("output token limit outside allowed bounds")
        client = GatewayClient(base_url=args.base_url, model=args.model, api_key=key, timeout=args.timeout)
        info = read_model_info(client)
        report = run_demo(client, info, args.include_portfolio, args.max_tokens)
        output.parent.mkdir(parents=True, exist_ok=True)
        # The completed artifact is created only after all selected model calls pass.
        with output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        print(json.dumps({"artifact": str(output), "actual_model_generation_calls": len(report["roles"]),
                          "input_tokens": report["total_reported_input_tokens"],
                          "output_tokens": report["total_reported_output_tokens"],
                          "market_backtest_performed": False, "orders_submitted": 0}, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__,
                          "error_code": exc.code if isinstance(exc, VLLMError) else "demo_failed",
                          "artifact_created": False}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
