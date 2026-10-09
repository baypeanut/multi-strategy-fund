# ML and research engineering

This project studies whether different decision processes improve a paper portfolio. It separates model inference, research evaluation, risk decisions, accounting, and execution. A successful inference response is not evidence of a profitable strategy.

## Code map

| Area | Responsibility | Boundary |
| --- | --- | --- |
| `core.data` | Equity, crypto, macro, and universe providers | Returns price histories; provider identity and missing data matter to interpretation. |
| `systems.s1_quant` | Deterministic signals, covariance, and portfolio construction | Control book; no LLM calls. |
| `systems.s2_news` | Headline ingestion, deduplication, sentiment, and time decay | Model output becomes a signal, not an order. |
| `systems.s3_llm` | Structured briefings, PM proposals, and proposal sanitization | Permitted symbols and weight checks precede shared risk processing. |
| `systems.vllm_provider` | Optional validated gateway integration | Explicit vLLM selection falls back to deterministic code, rather than another paid model. |
| `systems.s4_combined` | Combine the books and net symbol exposures | Uses shared risk normalization; no independent broker access. |
| `systems.s5_event` | Registered event-study replica | Preserves its existing entry, exit, and breadth specification. |
| `core.risk` | Final execution limits and firm-level circuit breakers | Deterministic decisions; invalid numeric inputs cannot pass risk comparisons. |
| `core.ledger` | Fill accounting and self-financing shadow-book settlement | Invalid fills cannot partially modify cash, positions, or fill history. |
| `backtest` | Historical holdings, costs, and performance statistics | Strategy history must have unique, increasing timestamps. |
| `research` | Registered experiments, evaluation, and agent proposal workflow | Research results and confirmation state are distinct from model-written hypotheses. |

The serving gateway and Kubernetes deployment have their own operational documentation. Their health and benchmark results must be assessed separately from trading results.

## Model boundary

The model proposes portfolio weights, classifies sentiment, or proposes and critiques a research hypothesis. It has no order-submission interface. The optional vLLM path uses fixed output schemas, independent validation, token and request limits, timeouts, and authentication. An invalid response or unavailable model is attributed to the deterministic fallback.

Proposal checks do not replace execution checks. Shared volatility normalization and per-book execution limits are applied by the runtime. The S4 governor assesses marked equity and exposure; its halt decision is latched and applied to all books. Final settlement accounts for trading and borrow costs. Unpriced holdings cannot be assumed to disappear or fund new risk elsewhere.

`RiskWrapper` performs initial proposal sanitization. Its turnover interpolation can retain a previously infeasible holding; it should not be treated as the complete execution boundary in isolation. The runtime's `final_limits` and settlement path remain necessary. This review did not change that historical proposal protocol.

## Research evidence

The harness owns exploration and confirmation windows, deduplicates registered specifications, counts family trials, and records source/configuration/universe fingerprints. These mechanisms improve auditability. They do not establish that a selected hypothesis is profitable or remove every source of selection bias.

Historical windows already inspected during exploration are not untouched evidence. Confirmation state and historical result records must not be reset to obtain another favorable attempt. A change to evaluation code requires a new source fingerprint; it does not rewrite previous results.

The model-serving research demonstration uses explicitly synthetic inputs to exercise proposal, critique, and deterministic gates. Its outputs cannot authorize live trading. An API workload artifact records observed requests; a separate deployment verification must establish actual GPU allocation and inference.

## Important limitations

- The close-to-close backtest assumes execution at the decision close with modeled costs. It is not a next-open, latency-aware, or exchange queue simulation.
- A chronological backtest is not automatically out of sample. Universe selection, parameter selection, and previously inspected dates also need independent control.
- A current universe or subsequently adjusted price history can introduce survivorship and revision effects. Source hashes alone do not freeze external data.
- The basic EDGAR news client uses a filing calendar date, not an intraday acceptance timestamp. It cannot establish when a filing was tradable during that day.
- The current news decay helper clamps future timestamps to age zero. Future publication times must be excluded or independently checked before treating a historical news replay as causal. This review recorded the limitation instead of changing the registered signal protocol.
- The event-study bootstrap uses a module-level generator. Reproducibility requires recording call order or a controlled fresh process, as well as the seed. Existing recorded experiments were not rerun or regraded.
- The governor checks covariance values for finiteness. Coverage of every exposure and positive-semidefinite covariance assumptions require separate assessment; a finite matrix alone is not proof of a valid risk model.

## Engineering checks added in this review

1. Ledger fills validate numeric inputs, prices, fees, and computed accounting values before committing. Tests cover new and existing positions, arithmetic overflow, partial closing, and flipping direction.
2. Backtests reject ambiguous timestamps before calling a strategy. A regression test demonstrates why sorting only the valuation panel leaves an unsorted original history capable of revealing a future row.
3. The governor returns finite cash targets and a halt for invalid proposals, equity histories, or relevant covariance values. Tests preserve the existing valid-input VaR calculation and the cash-book case.
4. The engineer's context includes current serving and deployment source. An archived ETF copy is listed explicitly by path, hash, and size instead of being presented as executing fund source. The context cap remains a character heuristic, not a measured tokenizer capacity.

The tests use synthetic fixtures and local doubles. They verify software behavior and accounting identities, not market profitability. These source changes were not deployed to the existing trading service and did not modify historical ledgers, strategy parameters, or research clocks.
