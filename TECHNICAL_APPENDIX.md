# Technical Appendix - Models, Data, Formulas

The formulas the implementation uses, no hand-waving. Notation: `r_t` log return,
`σ` volatility, `Σ` covariance, `BPS = 1e-4`.

## Implementation status (honesty table - kept current)

| Component | Status |
|---|---|
| EWMA / GARCH(1,1) / Yang-Zhang vol | **implemented** (engine uses Yang-Zhang; EWMA/GARCH written & tested, not the default) |
| Momentum 12-1, short-term reversal, low-vol | **implemented & live** |
| IC-weighted signal combining | **written-not-wired** - engine uses fixed weights {mom 1.0, rev 0.5, lv 0.5} |
| OU half-life filter | **written-not-wired** |
| Pairs / cointegration (Engle-Granger) | **planned** - formula only, no code |
| Ledoit-Wolf shrinkage covariance | **implemented & live** |
| ERC risk parity | **written-not-wired** - live books use signal-portfolio + vol-target path |
| Fractional Kelly | **written-not-wired** (vol-targeting is the live sizing rule) |
| Vol-targeting + caps (all four books) | **implemented & live** (W3) |
| Square-root impact cost model | **implemented** in backtest/PaperBroker; live shadow accounting uses spread+commission only (impact negligible at $3M - documented divergence) |
| SUE / PEAD | **written-not-wired** (needs estimate data) |
| Market-model event study (CAR) | **implemented** (research script), not a live signal |
| News pipeline (dedup → tiered scoring → decay → z) | **implemented & live** (W4b) |
| S3 LLM PM behind hard wrapper | **implemented & live** (W4a, qwen2.5:3b via Ollama, `pm_source` audited) |
| DSR / PBO / walk-forward | **implemented** (validation scripts) |
| Paired DM test w/ Newey-West | **implemented & live** (W3; server computes S3-vs-S1 nightly readout) |
| Daily-resampled governor gates | **implemented & live** (W1) |
| Data-coverage guard + alerts | **implemented & live** (W2) |
| Sector concentration cap (25%) | **implemented & live** (governor; audit gap closed 2026-07-05) |
| Anthropic PM/Scorer chain (SDK, structured outputs) | **implemented - activates on ANTHROPIC_API_KEY** (fallback: ollama -> heuristic/lexicon) |
| Polygon data provider + factory | **implemented - activates on POLYGON_API_KEY** (fallback: yfinance) |
| Sharadar PIT provider (research) | **implemented - activates on NASDAQ_DATA_LINK_API_KEY** |
| IBKR paper execution | **blocked on account** - the one integration that cannot be built untestable; adapter seam documented, wired after IBKR approval |

## A. Free data sources (proof phase, ~$0)
| Layer | Source | Cost |
|---|---|---|
| Equity price/hist | yfinance, Stooq | $0 |
| Crypto price/depth | ccxt public (binanceus default) | $0 |
| Filings | SEC EDGAR (8-K/10-Q) | $0 |
| News | GDELT, NewsAPI free, RSS | $0 |
| Macro/regime | FRED CSV endpoint (no key) | $0 |
| LLM (S2/S3) | local Ollama / cheap model | ~$0 |

Data hygiene: split/div-adjusted, point-in-time fundamentals (no look-ahead),
winsorize ±5σ, survivorship-aware history.

## B. System 1 - Quant (implemented in step 2)
**Returns/vol**
- `r_t = ln(P_t/P_{t-1})`
- EWMA (λ=0.94): `σ²_t = λσ²_{t-1} + (1-λ)r²_{t-1}`
- GARCH(1,1): `σ²_t = ω + αε²_{t-1} + βσ²_{t-1}`
- Yang-Zhang: `σ²_YZ = σ²_over + kσ²_oc + (1-k)σ²_RS`,
  `σ²_RS = mean[ln(H/C)ln(H/O) + ln(L/C)ln(L/O)]`

**Signals → cross-sectional z-scores**
- Momentum 12-1: `M_i = P_i(t-21)/P_i(t-252) − 1`, risk-adj `M̃_i = M_i/σ_i`
- Short-term MR: `z^mr_i = −(P_i − SMA_n)/σ_n`
- OU half-life filter: fit AR(1), `θ=−ln(b)/Δt`, `H=ln2/θ`, trade if 5<H<30d
- Low-vol: `z^lv_i = −z(σ_i)`

**Combine (IC-weighted):** `S_i = Σ_s w_s z^s_i`, `w_s ∝ IC_s/Σ|IC|`,
`IC_s = corr(signal_s, r_fwd)` (Spearman, rolling); drop negative-IC signals.

**Covariance (Ledoit-Wolf):** `Σ̂ = δF + (1−δ)S`, F = constant-correlation target.

**Sizing/portfolio**
- Inverse-vol: `w_i = (1/σ_i)/Σ(1/σ_j)`
- ERC (risk parity): `w_i·(Σ̂w)_i` equal ∀i (numeric)
- Mean-variance QP: `max wᵀS − (λ/2)wᵀΣ̂w` s.t. limits; closed form `w*=(1/λ)Σ̂⁻¹S`
- Vol target: `w_final = w·(σ_target/σ_port)`, `σ_port=√(wᵀΣ̂w)·√252`, σ_target=0.10
- Fractional Kelly: `f*=μ/σ²`, use `0.25·f*`

**Crypto:** TSMOM `sign(r(t−k,t))·(σ_target/σ_i)`; carry `funding_8h×3×365`.
**Pairs (cointegration):** Engle-Granger OLS `y=α+βx+e`, ADF(e); trade `z` of
spread, enter |z|>2, exit z→0, stop |z|>3.5.

## C. System 2 - News/Event (step 4)
- Pipeline: ingest → embed-dedup (cosine>0.9) → entity link → triage → score → decay
- SUE = `(EPS_act − EPS_cons)/σ(estimates)`; PEAD drift ~40–60d in SUE direction
- Event study (market model): `R_i=α+βR_mkt+ε` (est −250..−20),
  `AR=R_act−(α̂+β̂R_mkt)`, `CAR=ΣAR`
- Sentiment: `S_i(t)=Σ_k w_k s_k exp(−(t−t_k)/τ)`
- Guardrails: universe-only, citation check (no fact not in input → reject)

## D. System 3 - LLM Discretionary (step 5)
- Structured briefing (prices, technicals, fundamentals, S2 digest, regime).
  Regime: trend `P>SMA200`, breadth, VIX percentile, term `VIX/VIX3M`.
- LLM returns target portfolio + rationale (JSON) → **hard wrapper**: schema +
  limit + liquidity + citation + turnover checks. LLM never sends orders directly.

## E. System 4 - Combined (step 5)
- Start: inverse-vol risk-parity across S1/S2/S3.
- Then: min-variance `w=(Σ̂_sys⁻¹·1)/(1ᵀΣ̂_sys⁻¹1)`, Ledoit-Wolf shrunk.
- Net offsetting positions (S1 long + S2 short same name). No performance-chasing;
  slow Bayesian weight updates only after months.

## F. Risk & execution
- Parametric VaR: `VaR_α = −(μ_p − z_α σ_p)·NAV`
- Cornish-Fisher (skew/kurt): `z_CF = z + (z²−1)S/6 + (z³−3z)K/24 − (2z³−5z)S²/36`
- CVaR: `E[L | L>VaR_α]`
- Square-root impact: `impact_bps ≈ Y·σ_daily·√(Q/ADV)` *(implemented in `core/broker/costs.py`)*
- Drawdown: `DD=(Eq−maxEq)/maxEq`; gates −10%→×0.5, −15%→halt; −3%/day→kill

## G. Validation statistics (step 3)
- Sharpe `SR=(μ−rf)/σ` (×√252); `t≈SR·√T_yr`
- **Deflated Sharpe Ratio**: `PSR(SR*)=Φ((SR−SR*)√(T−1)/√(1−γ₃SR+((γ₄−1)/4)SR²))`,
  `SR* ≈ √Var(SR)·[(1−γ)Φ⁻¹(1−1/N)+γΦ⁻¹(1−1/(Ne))]`; require DSR>0.95
- **PBO** via CSCV; reject if >0.5
- Fundamental law: `IR ≈ IC·√breadth·TC`
- Walk-forward: [train 12m]→[test 3m]→roll; report OOS only, params frozen in test
