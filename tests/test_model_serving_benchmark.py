"""Real local HTTP exchanges test the load driver, never GPU/model performance."""
import asyncio
import csv
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
from pathlib import Path
import threading
import time
import uuid

import pytest

from scripts.benchmark_model_serving import (
    BenchmarkConfig, RequestResult, _strict_json, artifact_paths, main,
    percentile, resolve_provenance, run_benchmark, summarize, validate_response,
    write_artifacts,
)


def valid_response(index=0):
    return {
        "request_id": str(uuid.UUID(int=index + 1)), "model": "test-double-sentiment",
        "task": "sentiment", "data": {"score": 0.0, "confidence": 0.5},
        "usage": {"input_tokens": 40, "output_tokens": 12},
        "latency_seconds": 0.001,
    }


class ContractServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128


@pytest.fixture
def contract_server():
    state = {"lock": threading.Lock(), "plan": [], "seen": [],
             "active": 0, "max_active": 0, "info_status": 200,
             "info": {"backend": "test-double", "model": "test-double-sentiment",
                      "deployment_environment": "test-double", "model_revision": "fixture"}}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def reply(self, status, raw):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            self.reply(state["info_status"], json.dumps(state["info"]).encode())

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with state["lock"]:
                index = len(state["seen"])
                state["seen"].append({"body": payload, "auth": self.headers.get("Authorization")})
                mode = state["plan"][index] if index < len(state["plan"]) else "ok"
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            try:
                if mode == "slow-stream":
                    raw = json.dumps(valid_response(index)).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    for start in range(0, len(raw), 15):
                        try:
                            self.wfile.write(raw[start:start + 15])
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            break
                        time.sleep(0.04)
                    return
                if mode == "timeout":
                    time.sleep(0.5)
                else:
                    time.sleep(0.02)
                body = valid_response(index)
                if mode == "schema":
                    body["data"]["confidence"] = 2
                elif mode == "usage":
                    del body["usage"]
                elif mode == "model":
                    body["model"] = "unexpected-model"
                elif mode == "huge-usage":
                    body["usage"]["output_tokens"] = 1000000
                if mode == "status":
                    self.reply(429, b'{"error":"do not save this body"}')
                elif mode == "json":
                    self.reply(200, b'not-json')
                elif mode == "oversize":
                    self.reply(200, b' ' * (1024 * 1024 + 1))
                else:
                    self.reply(200, json.dumps(body).encode())
            finally:
                with state["lock"]:
                    state["active"] -= 1

    server = ContractServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = "http://127.0.0.1:" + str(server.server_port)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def config(server, **kwargs):
    return replace(BenchmarkConfig(server["url"], concurrency=(1,), requests_per_level=4,
                                   warmup_requests=0, timeout_seconds=2,
                                   evidence_kind="test-double"), **kwargs)


def test_actual_http_concurrency_warmup_and_token_totals(contract_server):
    cfg = config(contract_server, concurrency=(1, 4), warmup_requests=2)
    report = asyncio.run(run_benchmark(cfg, "not-a-real-key"))
    assert report["provenance"]["evidence_kind"] == "TEST_DOUBLE_CONTRACT"
    assert report["provenance"]["gpu_execution_verified_by_driver"] is False
    assert report["total_attempts_including_warmup"] == 10
    assert report["total_measured_attempts"] == 8
    assert report["warmup"]["attempted_requests"] == 2
    assert report["warmup"]["concurrency"] == 1
    assert contract_server["max_active"] == 4
    assert len(contract_server["seen"]) == 10
    for sent in contract_server["seen"]:
        assert sent["auth"] == "Bearer not-a-real-key"
        assert sent["body"]["task"] == "sentiment"
        assert sent["body"]["temperature"] == 0
    for stage in report["stages"]:
        assert stage["counts_reconcile"]
        assert stage["successful_requests"] == stage["attempted_requests"] == 4
        assert stage["reported_input_tokens"] == 160
        assert stage["reported_output_tokens"] == 48
        assert stage["successful_requests_per_second"] == 4 / stage["elapsed_seconds"]
        assert stage["all_attempt_latency_seconds"]["p50"] > 0
    assert report["metadata"]["retry_count"] == 0
    assert report["metadata"]["httpx_version"] == "0.28.1"


def test_failures_stay_in_denominator_and_no_hidden_retry(contract_server):
    contract_server["plan"] = ["ok", "status", "json", "schema"]
    report = asyncio.run(run_benchmark(config(contract_server, concurrency=(4,)), "private-test-key"))
    stage = report["stages"][0]
    assert len(contract_server["seen"]) == 4
    assert stage["attempted_requests"] == 4
    assert stage["successful_requests"] == 1
    assert stage["failed_requests"] == 3
    assert stage["success_rate"] == 0.25
    assert stage["outcomes"]["http_error"] == 1
    assert stage["outcomes"]["json_error"] == 1
    assert stage["outcomes"]["schema_error"] == 1
    assert stage["http_status_counts"] == {"200": 3, "429": 1}
    # Malformed sentiment still consumes reported backend tokens.
    assert stage["usage_reporting_requests"] == 2
    assert stage["reported_output_tokens"] == 24
    assert stage["successful_output_tokens_per_second"] == 12 / stage["elapsed_seconds"]
    assert stage["usage_missing_requests"] == 2
    evidence = json.dumps(report)
    assert "private-test-key" not in evidence
    assert "do not save this body" not in evidence
    assert cfg_prompt_hash(report) == report["metadata"]["workload"]["prompt_sha256"]


def cfg_prompt_hash(report):
    import hashlib
    from scripts.benchmark_model_serving import DEFAULT_PROMPT
    return hashlib.sha256(DEFAULT_PROMPT.encode()).hexdigest()


def test_hard_deadline_stops_slow_stream_not_just_inactivity(contract_server):
    contract_server["plan"] = ["slow-stream"]
    started = time.perf_counter()
    report = asyncio.run(run_benchmark(config(contract_server, requests_per_level=1,
                                             timeout_seconds=0.1), "dummy"))
    elapsed = time.perf_counter() - started
    assert report["stages"][0]["outcomes"]["timeout"] == 1
    assert report["requests"][0]["latency_seconds"] < 0.35
    assert elapsed < 0.5
    assert len(contract_server["seen"]) == 1


def test_missing_usage_model_mismatch_and_oversize_are_failures(contract_server):
    contract_server["plan"] = ["usage", "model", "oversize", "ok"]
    report = asyncio.run(run_benchmark(config(contract_server), "dummy"))
    stage = report["stages"][0]
    assert stage["outcomes"]["schema_error"] == 2
    assert stage["outcomes"]["response_too_large"] == 1
    assert stage["successful_requests"] == 1
    issues = {r["schema_issue"] for r in report["requests"]}
    assert {"invalid_usage", "model_mismatch"} <= issues
    assert stage["usage_missing_requests"] == 2


def test_failed_preflight_sends_no_inference(contract_server):
    contract_server["info_status"] = 401
    with pytest.raises(ValueError, match="no inference workload sent"):
        asyncio.run(run_benchmark(config(contract_server), "dummy"))
    assert not contract_server["seen"]


def test_impossible_token_usage_is_unknown_not_inflated_throughput(contract_server):
    contract_server["plan"] = ["huge-usage", "ok"]
    report = asyncio.run(run_benchmark(config(contract_server, requests_per_level=2), "dummy"))
    stage = report["stages"][0]
    assert stage["outcomes"]["schema_error"] == 1
    assert stage["usage_missing_requests"] == 1
    assert stage["reported_output_tokens"] == 12
    assert report["requests"][0]["schema_issue"] == "invalid_usage"
    assert report["requests"][0]["output_tokens"] is None


def test_reported_token_counts_obey_declared_context_limit():
    body = valid_response()
    assert validate_response(body, body["model"], 128, 4096) is None
    assert validate_response(body, body["model"], 8, 4096) == "invalid_usage"
    assert validate_response(body, body["model"], 128, 32) == "invalid_usage"


def test_deployment_environment_is_a_claim_not_verified_gpu():
    cfg = BenchmarkConfig("http://127.0.0.1:8000", evidence_kind="api-only")
    provenance = resolve_provenance(cfg, {"backend": "vllm", "model": "fund-llm",
                                         "deployment_environment": "gpu-kubernetes"})
    assert provenance["evidence_kind"] == "MODEL_API_WORKLOAD"
    assert provenance["gpu_execution_verified_by_driver"] is False
    with pytest.raises(ValueError, match="explicit"):
        resolve_provenance(cfg, {"backend": "vllm", "model": "fund-llm", "deployment_environment": "test-double"})
    with pytest.raises(ValueError, match="requires"):
        resolve_provenance(replace(cfg, evidence_kind="test-double"),
                           {"backend": "vllm", "model": "fund-llm", "deployment_environment": "gpu-kubernetes"})


def test_success_latency_does_not_hide_failure_latency():
    results = [RequestResult("measured", 2, 0, "ok", 0.1, input_tokens=20, output_tokens=10),
               RequestResult("measured", 2, 1, "timeout", 2.0)]
    stage = summarize(results, 2)
    assert stage["all_attempt_latency_seconds"]["p50"] == 1.05
    assert stage["successful_latency_seconds"]["p95"] == 0.1
    assert stage["attempts_per_second"] == 1
    assert stage["successful_requests_per_second"] == 0.5
    assert stage["usage_missing_requests"] == 1
    assert stage["reported_output_tokens_per_second"] == 5
    assert percentile([], 0.5) is None
    assert percentile([0, 10], 0.95) == 9.5


@pytest.mark.parametrize("body", [None, [], {}, {**valid_response(), "usage": {"input_tokens": True, "output_tokens": 2}},
    {**valid_response(), "latency_seconds": math.nan},
    {**valid_response(), "data": {"score": False, "confidence": 0.5}},
    {**valid_response(), "data": {"score": 0, "confidence": 0.5, "extra": "ignored"}},
])
def test_invalid_envelope_or_structured_output_rejected(body):
    assert validate_response(body, "test-double-sentiment") is not None


@pytest.mark.parametrize("raw", [b'{"x": NaN}', b'{"x":1,"x":2}', b'{"x": Infinity}'])
def test_nonfinite_and_duplicate_json_not_accepted(raw):
    with pytest.raises(ValueError):
        _strict_json(raw)


@pytest.mark.parametrize("overrides", [
    {"concurrency": ()}, {"concurrency": (1, 1)}, {"concurrency": (129,)},
    {"concurrency": (8,), "requests_per_level": 4}, {"timeout_seconds": math.inf},
    {"timeout_seconds": 0.001}, {"requests_per_level": 0}, {"warmup_requests": -1},
    {"max_tokens": 5000}, {"prompt": " "}, {"evidence_kind": "gpu-verified"},
    {"base_url": "http://user:password@example.com"}, {"base_url": "https://example.com?secret=x"},
    {"base_url": "file:///private/secret"},
])
def test_unsafe_or_unbounded_config_rejected(overrides):
    with pytest.raises(ValueError):
        replace(BenchmarkConfig("http://127.0.0.1:8000"), **overrides).validate()


def test_json_and_csv_evidence_reconcile_and_existing_evidence_preserved(contract_server, tmp_path):
    report = asyncio.run(run_benchmark(config(contract_server, warmup_requests=1), "secret-not-written"))
    prefix = tmp_path / "benchmark_test_double"
    json_path, summary_path, request_path = write_artifacts(report, prefix)
    saved = json.loads(json_path.read_text())
    summary = list(csv.DictReader(io.StringIO(summary_path.read_text())))
    requests = list(csv.DictReader(io.StringIO(request_path.read_text())))
    assert len(requests) == saved["total_attempts_including_warmup"] == 5
    assert [row["phase"] for row in summary] == ["warmup", "measured"]
    assert sum(int(row["attempted_requests"]) for row in summary) == 5
    assert int(summary[1]["successful_requests"]) == 4
    for path in artifact_paths(prefix):
        assert "secret-not-written" not in path.read_text()
    original = json_path.read_bytes()
    with pytest.raises(FileExistsError):
        write_artifacts(report, prefix)
    assert original == json_path.read_bytes()


def test_cli_missing_secret_returns_failure_without_exposing_env(contract_server, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("NO_SUCH_MODEL_KEY", raising=False)
    status = main(["--base-url", contract_server["url"], "--evidence-kind", "test-double",
                   "--api-key-env", "NO_SUCH_MODEL_KEY", "--output-prefix", str(tmp_path / "benchmark")])
    assert status == 1
    assert "error_type" in capsys.readouterr().err
    assert not contract_server["seen"]


def test_research_demo_deterministic_fixture_cannot_authorize_live():
    from scripts.demo_model_research import SYNTHETIC_FIXTURE, evaluate_synthetic_fixture
    evaluated = evaluate_synthetic_fixture(SYNTHETIC_FIXTURE)
    assert evaluated["fixture_only"] is True
    assert evaluated["is_real_backtest"] is False
    assert evaluated["sample_trades"] == 12
    assert evaluated["gross_trade_return_sum_bps"] == 25
    assert evaluated["modeled_cost_sum_bps"] == 120
    assert evaluated["net_trade_return_sum_bps"] == -95
    assert evaluated["deterministic_gate"]["eligible_for_live_trading"] is False
    assert evaluated["deterministic_gate"]["llm_verdict_can_override_gate"] is False


def test_research_demo_runs_separate_roles_and_existing_risk_gate():
    from core.llm import GenerationResult
    from scripts.demo_model_research import run_demo

    class FakeClient:
        base_url = "http://test-double.invalid/v1"
        calls = []

        def generate(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["task"] == "portfolio":
                data = {"positions": [{"symbol": "SPY", "weight": 0.04},
                                      {"symbol": "QQQ", "weight": -0.04}]}
            else:
                data = {"hypothesis": "Test only", "rules": ["Next bar only"],
                        "risks": ["Synthetic evidence"],
                        "verdict": "test" if len(self.calls) == 1 else "insufficient_evidence",
                        "confidence": 0.4}
            return GenerationResult(data, 200, 25, "test-only-not-GPU", f"fixture-{len(self.calls)}")

    client = FakeClient()
    report = run_demo(client, {"backend": "test-double", "model": "test-only-not-GPU"}, include_portfolio=True)
    assert [r["role"] for r in report["roles"]] == ["research_proposer", "research_critic", "paper_portfolio_proposer"]
    assert [call["task"] for call in client.calls] == ["research", "research", "portfolio"]
    assert "SYNTHETIC" in client.calls[1]["prompt"]
    assert "net_trade_return_sum_bps" in client.calls[1]["prompt"]
    assert report["total_reported_input_tokens"] == 600
    assert report["total_reported_output_tokens"] == 75
    assert report["provenance"]["market_backtest_performed"] is False
    assert report["provenance"]["orders_submitted"] == 0
    assert report["provenance"]["ledger_writes"] == 0
    gate = report["risk_wrapper_demo"]
    assert all(gate["post_gate_checks"].values())
    assert gate["raw_model_proposal"] == {"SPY": 0.04, "QQQ": -0.04}
    assert gate["violations"]
    assert sum(abs(w) for w in gate["sanitized_weights"].values()) <= 0.01 + 1e-12
    assert gate["live_trading_eligible"] is False


def test_research_demo_model_info_rejects_test_double_and_whitelists_metadata():
    from scripts.demo_model_research import read_model_info

    class FakeClient:
        model = "fund-llm"
        environment = "test-double"

        def model_info(self):
            return {"model": self.model, "backend": "vllm", "deployment_environment": self.environment,
                    "model_revision": "unknown", "secret": "must-never-be-recorded"}

    client = FakeClient()
    with pytest.raises(ValueError, match="non-test-double"):
        read_model_info(client)
    client.environment = "gpu-kubernetes"
    info = read_model_info(client)
    assert info["deployment_environment"] == "gpu-kubernetes"
    assert "secret" not in info
