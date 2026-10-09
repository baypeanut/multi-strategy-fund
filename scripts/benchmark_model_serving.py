#!/usr/bin/env python3
"""Bounded concurrent HTTP benchmark for the fund's structured generation API.

This measures the endpoint that actually answers. A local test double validates
this load driver only; it is never evidence of vLLM, GPU, or model performance.
Install requirements-serving.txt before running. No credentials or prompt/output
contents are written to artifacts, and failed attempts are never discarded.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
import uuid

DEFAULT_PROMPT = (
    "Classify the sentiment of this research-only news statement: "
    "The company reported revenue in line with guidance and left its annual "
    "forecast unchanged. Return the requested score and confidence only."
)
MAX_RESPONSE_BYTES = 1024 * 1024
OUTCOMES = (
    "ok", "http_error", "timeout", "connection_error", "json_error",
    "schema_error", "response_too_large", "client_error",
)
MODEL_INFO_FIELDS = (
    "backend", "model", "deployment_environment", "model_revision",
    "vllm_version", "limits",
)
MODEL_INFO_LIMIT_FIELDS = (
    "max_concurrency", "max_context_tokens", "max_output_tokens",
    "max_prompt_chars", "request_timeout_seconds",
)


@dataclass(frozen=True)
class BenchmarkConfig:
    base_url: str
    concurrency: tuple[int, ...] = (1, 4, 8, 16)
    requests_per_level: int = 32
    warmup_requests: int = 4
    timeout_seconds: float = 60.0
    max_tokens: int = 128
    prompt: str = DEFAULT_PROMPT
    evidence_kind: str = "api-only"

    def validate(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base URL must not contain credentials, query or fragment")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("invalid base URL port") from exc
        if not self.concurrency or len(set(self.concurrency)) != len(self.concurrency):
            raise ValueError("concurrency levels must be nonempty and unique")
        if any(type(c) is not int or not 1 <= c <= 128 for c in self.concurrency):
            raise ValueError("each concurrency level must be between 1 and 128")
        if type(self.requests_per_level) is not int or not 1 <= self.requests_per_level <= 10000:
            raise ValueError("requests per level must be between 1 and 10000")
        if max(self.concurrency) > self.requests_per_level:
            raise ValueError("requests per level must be at least the largest concurrency")
        if type(self.warmup_requests) is not int or not 0 <= self.warmup_requests <= 1000:
            raise ValueError("warmup requests must be between 0 and 1000")
        if not _finite_number(self.timeout_seconds) or not 0.1 <= self.timeout_seconds <= 300:
            raise ValueError("timeout must be between 0.1 and 300 seconds")
        if type(self.max_tokens) is not int or not 8 <= self.max_tokens <= 1024:
            raise ValueError("max tokens must be between 8 and 1024")
        if not isinstance(self.prompt, str) or not self.prompt.strip() or len(self.prompt) > 24000:
            raise ValueError("prompt must contain between 1 and 24000 characters")
        if self.evidence_kind not in {"test-double", "api-only"}:
            raise ValueError("evidence kind must be test-double or api-only")


@dataclass
class RequestResult:
    phase: str
    concurrency: int
    sequence: int
    outcome: str
    latency_seconds: float
    http_status: int | None = None
    response_bytes: int = 0
    request_id: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    backend_latency_seconds: float | None = None
    error_type: str | None = None
    schema_issue: str | None = None


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _token_count(value: Any) -> bool:
    return type(value) is int and value >= 0


def percentile(values: list[float], q: float) -> float | None:
    """Linearly interpolate the empirical quantile (including endpoints)."""
    if not values:
        return None
    if not 0 <= q <= 1:
        raise ValueError("quantile must be in [0,1]")
    ranked = sorted(values)
    position = (len(ranked) - 1) * q
    lo, hi = math.floor(position), math.ceil(position)
    return ranked[lo] + (ranked[hi] - ranked[lo]) * (position - lo)


def _strict_json(raw: bytes) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError("non-finite JSON constant")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(raw, parse_constant=reject_constant, object_pairs_hook=reject_duplicates)


def validate_response(body: Any, expected_model: str, max_output_tokens: int | None = None,
                      max_input_tokens: int | None = None) -> str | None:
    """Check both the gateway envelope and the fixed sentiment output contract."""
    if not isinstance(body, dict):
        return "envelope_not_object"
    if not isinstance(body.get("request_id"), str) or not body["request_id"].strip():
        return "missing_request_id"
    try:
        uuid.UUID(body["request_id"])
    except (ValueError, AttributeError):
        return "invalid_request_id"
    if body.get("model") != expected_model:
        return "model_mismatch"
    if body.get("task") != "sentiment":
        return "task_mismatch"
    latency = body.get("latency_seconds")
    if not _finite_number(latency) or latency < 0:
        return "invalid_backend_latency"
    usage = body.get("usage")
    if not isinstance(usage, dict) or not all(_token_count(usage.get(k)) for k in ("input_tokens", "output_tokens")):
        return "invalid_usage"
    if ((max_output_tokens is not None and usage["output_tokens"] > max_output_tokens)
            or (max_input_tokens is not None and usage["input_tokens"] > max_input_tokens)):
        return "invalid_usage"
    data = body.get("data")
    if not isinstance(data, dict) or set(data) != {"score", "confidence"}:
        return "invalid_sentiment_fields"
    if not _finite_number(data["score"]) or not -1 <= data["score"] <= 1:
        return "invalid_score"
    if not _finite_number(data["confidence"]) or not 0 <= data["confidence"] <= 1:
        return "invalid_confidence"
    return None


def summarize(results: list[RequestResult], elapsed: float) -> dict[str, Any]:
    if elapsed <= 0 or not math.isfinite(elapsed):
        raise ValueError("elapsed must be finite and positive")
    counts = {outcome: sum(r.outcome == outcome for r in results) for outcome in OUTCOMES}
    if sum(counts.values()) != len(results):
        raise ValueError("unrecognized outcome prevents reconciliation")
    successful = [r for r in results if r.outcome == "ok"]
    reported = [r for r in results if r.input_tokens is not None and r.output_tokens is not None]
    statuses: dict[str, int] = {}
    for result in results:
        if result.http_status is not None:
            status = str(result.http_status)
            statuses[status] = statuses.get(status, 0) + 1
    latencies = [r.latency_seconds for r in results]
    valid_latencies = [r.latency_seconds for r in successful]
    inputs = sum(r.input_tokens or 0 for r in reported)
    outputs = sum(r.output_tokens or 0 for r in reported)
    valid_outputs = sum(r.output_tokens or 0 for r in successful)
    return {
        "attempted_requests": len(results),
        "successful_requests": len(successful),
        "failed_requests": len(results) - len(successful),
        "success_rate": len(successful) / len(results) if results else None,
        "outcomes": counts,
        "http_status_counts": statuses,
        "http_responses": sum(statuses.values()),
        "elapsed_seconds": elapsed,
        "attempts_per_second": len(results) / elapsed,
        "successful_requests_per_second": len(successful) / elapsed,
        "all_attempt_latency_seconds": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
            "max": max(latencies) if latencies else None,
        },
        "successful_latency_seconds": {
            "p50": percentile(valid_latencies, 0.50),
            "p95": percentile(valid_latencies, 0.95),
            "p99": percentile(valid_latencies, 0.99),
            "max": max(valid_latencies) if valid_latencies else None,
        },
        "usage_reporting_requests": len(reported),
        "usage_missing_requests": len(results) - len(reported),
        "reported_input_tokens": inputs,
        "reported_output_tokens": outputs,
        "reported_output_tokens_per_second": outputs / elapsed,
        "successful_output_tokens_per_second": valid_outputs / elapsed,
        "counts_reconcile": len(results) == len(successful) + (len(results) - len(successful)),
    }


async def _read_bounded_response(client: Any, method: str, url: str,
                                 observed: RequestResult | None = None,
                                 **kwargs: Any) -> tuple[int, bytes]:
    async with client.stream(method, url, **kwargs) as response:
        if observed is not None:
            observed.http_status = response.status_code
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if observed is not None:
                observed.response_bytes = len(raw)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ResponseTooLarge()
        return response.status_code, bytes(raw)


class ResponseTooLarge(Exception):
    pass


async def perform_request(client: Any, config: BenchmarkConfig, expected_model: str,
                          phase: str, concurrency: int, sequence: int,
                          max_input_tokens: int | None = None) -> RequestResult:
    import httpx

    start = time.perf_counter()
    result = RequestResult(phase, concurrency, sequence, "client_error", 0.0)
    payload = {"task": "sentiment", "prompt": config.prompt,
               "max_tokens": config.max_tokens, "temperature": 0.0}
    try:
        status, raw = await asyncio.wait_for(
            _read_bounded_response(client, "POST", config.base_url.rstrip("/") + "/v1/generate",
                                   observed=result, json=payload),
            timeout=config.timeout_seconds,
        )
        result.http_status, result.response_bytes = status, len(raw)
        if status != 200:
            result.outcome = "http_error"
            return result
        try:
            body = _strict_json(raw)
        except (ValueError, UnicodeError):
            result.outcome = "json_error"
            return result
        if isinstance(body, dict):
            # Preserve valid reported usage even when generated data fails schema.
            usage = body.get("usage")
            if (isinstance(usage, dict)
                    and all(_token_count(usage.get(k)) for k in ("input_tokens", "output_tokens"))
                    and usage["output_tokens"] <= config.max_tokens
                    and (max_input_tokens is None or usage["input_tokens"] <= max_input_tokens)):
                result.input_tokens = usage["input_tokens"]
                result.output_tokens = usage["output_tokens"]
            if isinstance(body.get("request_id"), str):
                result.request_id = body["request_id"][:128]
            if isinstance(body.get("model"), str):
                result.model = body["model"][:256]
            if _finite_number(body.get("latency_seconds")) and body["latency_seconds"] >= 0:
                result.backend_latency_seconds = body["latency_seconds"]
        result.schema_issue = validate_response(body, expected_model, config.max_tokens, max_input_tokens)
        result.outcome = "schema_error" if result.schema_issue else "ok"
    except (TimeoutError, httpx.TimeoutException):
        result.outcome = "timeout"
    except ResponseTooLarge:
        result.outcome = "response_too_large"
    except httpx.RequestError as exc:
        result.outcome = "connection_error"
        result.error_type = type(exc).__name__
    except Exception as exc:
        result.outcome = "client_error"
        result.error_type = type(exc).__name__
    finally:
        result.latency_seconds = time.perf_counter() - start
    return result


async def run_stage(client: Any, config: BenchmarkConfig, model: str, phase: str,
                    concurrency: int, requests: int,
                    max_input_tokens: int | None = None) -> tuple[list[RequestResult], dict[str, Any]]:
    queue: asyncio.Queue[int] = asyncio.Queue()
    for sequence in range(requests):
        queue.put_nowait(sequence)
    results: list[RequestResult] = []

    async def worker() -> None:
        while not queue.empty():
            try:
                sequence = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            result = await perform_request(client, config, model, phase, concurrency, sequence, max_input_tokens)
            results.append(result)
            queue.task_done()

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(min(concurrency, requests))))
    elapsed = time.perf_counter() - started
    results.sort(key=lambda r: r.sequence)
    metrics = summarize(results, max(elapsed, 1e-12))
    metrics.update({"phase": phase, "concurrency": concurrency, "planned_requests": requests})
    metrics["counts_reconcile"] = metrics["counts_reconcile"] and requests == len(results)
    return results, metrics


def resolve_provenance(config: BenchmarkConfig, model_info: dict[str, Any]) -> dict[str, Any]:
    environment = model_info.get("deployment_environment", "unknown")
    backend = model_info.get("backend", "unknown")
    model = model_info.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model-info lacks a valid model identifier")
    is_double = environment == "test-double" or backend in {"test-double", "mock", "stub"}
    if is_double and config.evidence_kind != "test-double":
        raise ValueError("test-double backend requires explicit --evidence-kind test-double")
    if config.evidence_kind == "test-double" and not is_double:
        raise ValueError("test-double evidence requires backend or environment marked test-double")
    safe_info = {k: model_info[k] for k in MODEL_INFO_FIELDS
                 if k != "limits" and isinstance(model_info.get(k), str)}
    if isinstance(model_info.get("limits"), dict):
        safe_info["limits"] = {k: model_info["limits"][k] for k in MODEL_INFO_LIMIT_FIELDS
                               if _finite_number(model_info["limits"].get(k))}
    return {
        "evidence_kind": "TEST_DOUBLE_CONTRACT" if is_double else "MODEL_API_WORKLOAD",
        "backend_claim": backend,
        "deployment_environment_claim": environment,
        "model_info": safe_info,
        "gpu_execution_verified_by_driver": False,
        "interpretation": (
            "Tests load-driver and HTTP/schema contract only. Not vLLM, GPU, or model performance."
            if is_double else
            "Observed HTTP endpoint workload. Backend/model-info values are declarations; "
            "GPU execution and deployment require separate observed evidence."
        ),
    }


def _git_revision() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent.parent,
            text=True, stderr=subprocess.DEVNULL, timeout=2,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _metadata(config: BenchmarkConfig) -> dict[str, Any]:
    source = Path(__file__).resolve()
    return {
        "run_id": str(uuid.uuid4()),
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "httpx_version": importlib.metadata.version("httpx"),
        "git_revision": _git_revision(),
        "driver_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "base_url": urlunsplit(urlsplit(config.base_url)),
        "workload": {
            "task": "sentiment", "prompt_sha256": hashlib.sha256(config.prompt.encode()).hexdigest(),
            "prompt_characters": len(config.prompt), "default_prompt": config.prompt == DEFAULT_PROMPT,
            "max_tokens": config.max_tokens, "temperature": 0.0,
            "concurrency_levels": list(config.concurrency),
            "requests_per_level": config.requests_per_level, "warmup_requests": config.warmup_requests,
            "fixed_prompt_across_requests": True,
            "generalization_limit": "Repeated fixed prompt may benefit from prefix caching; not a representative news/research corpus.",
        },
        "timeout_seconds": config.timeout_seconds,
        "retry_count": 0,
        "arrival_pattern": "closed-loop: at most N in-flight requests; replacement on completion",
        "measurement_scope": "client end-to-end non-streaming response including queueing, network and schema validation",
        "metric_definitions": {
            "attempted_requests": "Every dispatched driver request, including connection failures and timeouts.",
            "successful_requests": "HTTP 200 with valid JSON, matching model, valid envelope and sentiment schema.",
            "elapsed_seconds": "Wall-clock stage span, including failed attempts and final outstanding completion.",
            "tokens": "Only backend-reported input/output token usage; missing values remain unknown, never estimated.",
            "latency": "Per-attempt dispatch to completed response/error; no omitted failures in all-attempt quantiles.",
            "warmup": "Separate sequential stage; excluded from measured-stage throughput and latency summaries.",
            "limits": "Bounded request count/concurrency, no redirects/retries, full exchange timeout and 1 MiB response cap.",
        },
    }


async def run_benchmark(config: BenchmarkConfig, api_key: str) -> dict[str, Any]:
    import httpx

    config.validate()
    if not isinstance(api_key, str) or not api_key.strip() or "\n" in api_key or "\r" in api_key:
        raise ValueError("API key must be a nonempty single-line secret from an environment variable")
    metadata = _metadata(config)
    max_concurrency = max(config.concurrency)
    transport = httpx.AsyncHTTPTransport(retries=0, limits=httpx.Limits(
        max_connections=max_concurrency, max_keepalive_connections=max_concurrency))
    async with httpx.AsyncClient(
        transport=transport, timeout=config.timeout_seconds, follow_redirects=False,
        headers={"Authorization": "Bearer " + api_key}, trust_env=False,
    ) as client:
        status, raw = await asyncio.wait_for(
            _read_bounded_response(client, "GET", config.base_url.rstrip("/") + "/v1/model-info"),
            timeout=config.timeout_seconds,
        )
        if status != 200:
            raise ValueError(f"model-info preflight failed with HTTP {status}; no inference workload sent")
        model_info = _strict_json(raw)
        if not isinstance(model_info, dict):
            raise ValueError("model-info must be a JSON object")
        provenance = resolve_provenance(config, model_info)
        model = model_info["model"]
        declared_limits = model_info.get("limits", {})
        max_input_tokens = declared_limits.get("max_context_tokens") if isinstance(declared_limits, dict) else None
        if not _token_count(max_input_tokens) or max_input_tokens < 1:
            max_input_tokens = None
        all_results: list[RequestResult] = []
        warmup: dict[str, Any] | None = None
        if config.warmup_requests:
            results, warmup = await run_stage(client, config, model, "warmup", 1, config.warmup_requests, max_input_tokens)
            all_results.extend(results)
        stages = []
        for concurrency in config.concurrency:
            results, summary = await run_stage(client, config, model, "measured", concurrency, config.requests_per_level, max_input_tokens)
            all_results.extend(results)
            stages.append(summary)
    return {
        "schema_version": 1, "metadata": metadata, "provenance": provenance,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "warmup": warmup, "stages": stages,
        "total_attempts_including_warmup": len(all_results),
        "total_measured_attempts": sum(s["attempted_requests"] for s in stages),
        "requests": [asdict(r) for r in all_results],
    }


def artifact_paths(prefix: Path) -> tuple[Path, Path, Path]:
    return tuple(Path(str(prefix) + suffix) for suffix in (".json", "_summary.csv", "_requests.csv"))


def write_artifacts(report: dict[str, Any], prefix: Path) -> tuple[Path, Path, Path]:
    paths = artifact_paths(prefix)
    if any(path.exists() for path in paths):
        raise FileExistsError("benchmark artifacts already exist; choose a fresh output prefix")
    paths[0].parent.mkdir(parents=True, exist_ok=True)
    summary_buffer = io.StringIO()
    rows = []
    for stage in ([report["warmup"]] if report["warmup"] is not None else []) + report["stages"]:
        row = {k: v for k, v in stage.items() if k not in {"outcomes", "http_status_counts", "all_attempt_latency_seconds", "successful_latency_seconds"}}
        row.update({"outcome_" + k: v for k, v in stage["outcomes"].items()})
        for label in ("all_attempt_latency_seconds", "successful_latency_seconds"):
            row.update({label + "_" + k: v for k, v in stage[label].items()})
        rows.append(row)
    writer = csv.DictWriter(summary_buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    request_buffer = io.StringIO()
    writer = csv.DictWriter(request_buffer, fieldnames=list(RequestResult.__dataclass_fields__))
    writer.writeheader()
    writer.writerows(report["requests"])
    payloads = (json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", summary_buffer.getvalue(), request_buffer.getvalue())
    # Exclusive creates preserve previous evidence; output contains no credentials.
    for path, payload in zip(paths, payloads):
        with path.open("x", encoding="utf-8", newline="") as handle:
            handle.write(payload)
    return paths


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Gateway URL, excluding /v1/generate")
    parser.add_argument("--evidence-kind", required=True, choices=("test-double", "api-only"),
                        help="Explicit test-double labeling; API-only never attests to GPU deployment")
    parser.add_argument("--concurrency", default="1,4,8,16")
    parser.add_argument("--requests-per-level", type=int, default=32)
    parser.add_argument("--warmup-requests", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--api-key-env", default="MODEL_API_KEY", help="Environment variable name, never the secret")
    parser.add_argument("--prompt-file", type=Path, help="Optional reproducible UTF-8 prompt; artifact records its hash only")
    parser.add_argument("--output-prefix", type=Path, default=Path("research/model_serving_20261009") / (
        "benchmark_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        concurrency = tuple(int(v.strip()) for v in args.concurrency.split(","))
        prompt = args.prompt_file.read_text(encoding="utf-8") if args.prompt_file else DEFAULT_PROMPT
        config = BenchmarkConfig(args.base_url, concurrency, args.requests_per_level,
                                 args.warmup_requests, args.timeout, args.max_tokens, prompt, args.evidence_kind)
        config.validate()
        if any(path.exists() for path in artifact_paths(args.output_prefix)):
            raise FileExistsError("benchmark artifacts already exist; choose a fresh output prefix")
        key = os.environ.get(args.api_key_env, "")
        report = asyncio.run(run_benchmark(config, key))
        paths = write_artifacts(report, args.output_prefix)
        print(json.dumps({"evidence_kind": report["provenance"]["evidence_kind"],
                          "artifacts": [str(p) for p in paths],
                          "measured_attempts": report["total_measured_attempts"],
                          "successful_requests": sum(s["successful_requests"] for s in report["stages"]),
                          "failed_requests": sum(s["failed_requests"] for s in report["stages"])}, indent=2))
        return 0 if all(s["failed_requests"] == 0 for s in report["stages"]) and (
            report["warmup"] is None or report["warmup"]["failed_requests"] == 0) else 2
    except Exception as exc:
        # Never emit HTTP error bodies, exception messages, keys or prompt contents.
        print(json.dumps({"error_type": type(exc).__name__,
                          "error": "Benchmark failed before evidence completed; check URL, dependency, key environment, model-info and output path."}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
