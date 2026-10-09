# Concurrent model-serving measurement

Status: a real GPU deployment and a concurrent API workload were independently verified on October 9, 2026. [GPU verification](gpu_verification.json) records actual CUDA execution, vLLM generation, ready workloads and a Bound model-cache PVC; [the API benchmark](benchmark_gpu_actual.json) records the measured requests. The included `benchmark_test_double_contract*` artifacts remain **TEST_DOUBLE_CONTRACT** evidence only and never establish LLM, vLLM, GPU or Kubernetes performance.

## Observed GPU workload

The verified demonstrator used one NVIDIA L4, vLLM 0.31.0 and the pinned Qwen2.5-1.5B-Instruct model. Each accepted fixed sentiment request reported 89 input tokens and 17 output tokens. Four sequential warmup requests succeeded and are excluded from this table. The measured sweep contains all 128 attempted requests, including 24 rejected requests.

| Concurrent requests | Attempted | Successful | HTTP 429 | Successful requests/s | Successful p95 latency |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 32 | 32 | 0 | 1.565 | 0.714 s |
| 4 | 32 | 32 | 0 | 5.729 | 0.811 s |
| 8 | 32 | 32 | 0 | 10.864 | 0.791 s |
| 16 | 32 | 8 | 24 | 9.276 | 0.860 s |

The gateway's eight-request admission limit intentionally returned HTTP 429 for 24 requests at concurrency 16. These remain failed attempts in the benchmark denominator; they are not relabeled successful or silently retried. Concurrency 1, 4 and 8 had no failed requests. No measured request produced a timeout, connection failure, JSON failure or output-schema failure. At concurrency 8, successful output throughput was 184.683 backend-reported tokens/s. The [summary CSV](benchmark_gpu_actual_summary.csv) and [per-request CSV](benchmark_gpu_actual_requests.csv) retain the exact values and all-attempt latency summaries.

These figures cover one short, fixed prompt and 17-token outputs, not varied news or long research completions. Repeated prompts may benefit from prefix caching. The model ran in eager mode; no conclusion is made about a different CUDA-graph/compilation configuration. Client timings include the localhost Kubernetes port-forward, the network path to the GPU cluster and gateway validation; they are not GPU-kernel timings. The short closed-loop run does not establish production arrival-rate capacity, sustained uptime, trading quality or profitability.

An initial metadata connection attempt before port forwarding was ready is recorded separately in [benchmark preflight](benchmark_preflight.json). It sent zero model requests and is not part of the 128-request measured workload. Hardware execution is established by the independent deployment record, not the gateway's environment declaration. [Cache persistence](cache_persistence.json) and [after-replacement GPU verification](gpu_verification_after_restart.json) separately record model-cache survival and successful serving after replacing the model pod. The [three-call research demonstration](research_demo.json) retains actual proposer, critic and portfolio outputs with explicitly synthetic evaluation inputs; it establishes no trading-performance claim.

[Final cloud cleanup](cloud_cleanup.json) records cluster deletion, zero demo GPU nodes and removal of the model-cache disk, registry and dedicated node identity. The GPU demonstration is complete and no continuous service uptime is claimed. Its recorded cost is a conservative estimate, not a verified invoice.

## Run against the deployed serving gateway

Install the repository's `requirements-serving.txt`. Put the gateway key in the environment variable `MODEL_API_KEY` using the project's secret-handling procedure; do not supply it as a command-line argument. The benchmark stores neither the key nor prompt/generated contents.

```bash
python scripts/benchmark_model_serving.py \
  --base-url http://127.0.0.1:18000 \
  --evidence-kind api-only \
  --concurrency 1,4,8,16 \
  --requests-per-level 32 \
  --warmup-requests 4 \
  --timeout 60 \
  --max-tokens 128
```

Use the actual gateway URL, for example a localhost Kubernetes port-forward. The default output prefix includes UTC time and is written under `research/model_serving_20261009/`. Existing evidence is not overwritten. A gateway marked `test-double` requires `--evidence-kind test-double`; the driver refuses to relabel its results as model measurements.

The HTTP workload uses `POST /v1/generate` with the fixed `sentiment` task, deterministic requested temperature, one reproducible prompt, and a bounded output token limit. `--prompt-file` accepts another UTF-8 prompt and stores its hash. Warmup is sequential and reported separately. Concurrency levels run in the requested order, with at most that number of requests in flight and the next dispatched upon completion. This closed-loop benchmark measures a fixed workload, not a production arrival process. Repeated prompts can benefit from prefix caching; generalizing its results to a varied financial-news corpus requires another run with a representative workload.

## What the artifacts contain

- JSON: timestamped run ID, driver SHA-256, repository revision, Python/HTTPX/platform versions, workload and timeout settings, safe model-info declarations, each request result, warmup and stage summaries.
- Summary CSV: stage concurrency, all attempted/successful/failed counts, failure categories, elapsed wall time, attempts and successful requests per second, input/output token totals, token-reporting coverage, output tokens per second, all-attempt and successful-only p50/p95/p99 latencies.
- Requests CSV: each warmup/measured attempt, its latency, HTTP status where observed, outcome, safe failure category, request ID, model name, backend-reported usage and backend-reported latency where valid.

Every dispatched attempt remains in the denominator, including timeouts, connection failures, non-200 status codes, invalid JSON, invalid structured data, missing token usage, model mismatches, and oversized responses. The driver performs no retries and follows no redirects. The whole HTTP exchange has a hard asynchronous deadline, including a slowly arriving body. Responses are limited to 1 MiB. A response succeeds only if the declared model matches `/v1/model-info` and the gateway envelope plus sentiment object pass validation. JSON NaN, infinity, duplicate keys, boolean-as-number values, and out-of-range sentiment/confidence are rejected.

Tokens are backend-reported counts, never character-based estimates. Valid reported usage is preserved when structured output fails, because a malformed completion can still consume model work. Missing usage remains unknown. The JSON makes both successful-output throughput and all-reported-output throughput explicit. Failed requests can distort successful-only quantiles, so all-attempt quantiles are also supplied. Exit code 0 means all inference attempts passed, 2 means the run completed with at least one warmup/measured failure, and 1 means no complete benchmark evidence was produced.

## Provenance limits

`/v1/model-info` is a configuration declaration. Even `deployment_environment: gpu-kubernetes` cannot establish which hardware processed requests. The driver therefore always records `gpu_execution_verified_by_driver: false`. To substantiate GPU/Kubernetes serving, retain independent observations of the deployed pod's container image/model revision, NVIDIA runtime/device plugin and allocatable resources, the requested GPU resource, model storage/PVC binding, ready status, GPU inventory, and vLLM runtime logs. Compare their timestamps with a new API workload report. Keep these artifacts separate from the included contract-smoke results.

## Local validation

```bash
python -m pytest tests/test_model_serving_benchmark.py -q
```

The tests make actual concurrent HTTP exchanges with an explicitly labeled local test double. They verify warmup separation, observed concurrency, token accounting, JSON/schema/HTTP failures, timeouts including slow streaming, oversized response handling, safe provenance labeling, missing credentials, artifact reconciliation, and preservation of previous evidence. These are implementation and load-driver tests only.

## Structured research and risk-gate demonstration

After a real vLLM gateway is ready, the separate demonstration makes one research proposal and one critique using the same fixed research JSON schema. An optional third call produces a paper portfolio proposal and invokes the existing deterministic `RiskWrapper` with an explicitly fabricated briefing. The portfolio remains an in-memory proposal: no broker, ledger, or trading service is called.

```bash
python scripts/demo_model_research.py \
  --base-url http://127.0.0.1:18000/v1 \
  --model fund-llm \
  --timeout 60 \
  --include-portfolio
```

The demonstration refuses a gateway marked test-double. `research_demo.json` is created only after all selected real gateway calls succeed and validate. It preserves each role's output, prompt hash, model, request ID, actual reported token usage, and observed client latency. Its fabricated evaluation fixture is labeled `SYNTHETIC_PIPELINE_FIXTURE_NOT_MARKET_DATA` and is never presented as a market backtest. Deterministic arithmetic computes gross trade-return sum, modeled costs, and net trade-return sum; a gate rejects live eligibility because the fixture is synthetic, not out-of-sample, too small, and not leakage-audited. The LLM's confidence or verdict cannot override that gate. Independent GPU/Kubernetes deployment evidence is still required; successful API calls alone do not verify GPU execution.
