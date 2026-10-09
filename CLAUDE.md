# Multi Strategy Systematic Fund

This is a Python paper-trading research project with five books: deterministic
quant, news/event, constrained LLM proposals, an ensemble, and event drift.
Profitability and a model-driven advantage remain unproven. Read README.md,
RESEARCH_LOG.md and the relevant audit/correction records before evaluating
strategy results. Historic success labels are not current validation.

## Engineering boundaries

- S1 remains an LLM-free control. Model output is a proposal, never an order.
- Keep the deterministic wrapper, risk governor, data-quality gates and broker
  paper-account checks in the execution path.
- Preserve registered strategy parameters, costs, evaluation rules and clocks.
  A new hypothesis or changed model needs a separately registered experiment.
- State migrations must be additive. Never reset paper books, rewrite fills,
  remove equity history, or backfill missed signals as if they were executed.
- Accounting must remain self-financing and reconcile cash, quantities, fees
  and marks. Reject malformed/nonfinite inputs before mutating the ledger.
- Degraded data must not authorize trading. Fallbacks and failed model calls
  need explicit source/error attribution and must respect daily call limits.
- Evaluate on held-out data. Preserve timing, causal availability and costs;
  do not change assumptions to improve reported returns. The registered
  60-day/paired-test rule is a necessary gate, not proof of profitability.

## Services and credentials

Operational identities, account numbers, server addresses and credentials do
not belong in public documentation. Local operator notes are kept in the
ignored data/engineering/ directory. Obtain deployment targets explicitly
from the operator environment; never infer production targets from examples.

The default fund provider is auto (existing Anthropic/Ollama/deterministic
fallback chain). The optional vllm provider is opt-in. The isolated GPU
serving demonstration does not change a measured paper book provider.
GPU/cloud resources need an explicit budget, deadline and verified cleanup;
deleting a pod does not stop GPU-node billing. An approved $20 demonstration
does not authorize persistent paid infrastructure.

Keep secrets in environment variables, local ignored files or deployment
Secrets. Never print them, include them in a build context, or save them with
benchmark evidence. IB Gateway credentials remain outside this repository.
Do not use offline checks to submit orders, send notifications, enable paid
model jobs or restart production services.

## Work and verification

Review the relevant implementation and tests before changing it. Prefer small,
testable modules and explicit failure states over a broad cosmetic rewrite.
Run the offline suite from a clean checkout with both fund and serving
dependencies. Keep historical research and generated evidence distinguishable
from executing source. Append material engineering/research changes to
RESEARCH_LOG.md; negative results and failed deployment attempts are evidence.

GPU deployment claims require Ready pods, actual CUDA execution and model
generation, correct model revision and a mounted persistent cache. Concurrent
load results must retain every failed attempt and separate warmup. Test doubles
and synthetic research fixtures never establish model throughput or an edge.
