"""LiveRuntime - the paper trading orchestrator (two-tier data architecture).

Every tick (hourly):
  LIGHT: one grouped-daily call (whole market's closes) + 8 crypto tickers
         -> marking prices + bar date. News ingest (RSS subset + EDGAR cycle).
         Fingerprint = bar date | quantized news signal.
  HEAVY (only when the fingerprint changes - new bar or material news):
         full parallel history fetch -> engines (S1..S4) -> new target books.

Books therefore only re-decide on NEW information; between decisions they hold
weights exactly (zero turnover cost, zero LLM spend) while equity still marks
to market hourly. The governor runs every tick regardless - drawdown gates
must be able to halt a held book too.
"""
from __future__ import annotations

import json
import os
import pickle
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from core.alerts import maybe_send_daily_digest, queue_note, send_telegram
from core.broker.costs import CostModel, CostParams
from core.config import CONFIG
from core.data.crypto import CryptoDataProvider
from core.data.factory import equity_provider_name, make_equity_provider
from core.data.macro import MacroDataProvider
from core.data.universe import (CRYPTO_UNIVERSE, EQUITY_UNIVERSE,
                                average_dollar_volume, rss_subset)
from core.env import has_key
from core.risk.governor import RiskGovernor
from systems.s1_quant.covariance import ledoit_wolf_cov
from systems.s1_quant.engine import QuantEngine, build_panels
from systems.s1_quant.portfolio import scale_to_target_vol
from systems.s1_quant.volatility import log_returns
from systems.s3_llm.briefing import build_briefing
from systems.s3_llm.engine import S3Engine
from systems.s3_llm.pm import AnthropicPM, HeuristicPM, OllamaPM
from systems.s3_llm.regime import compute_regime
from systems.s3_llm.wrapper import RiskWrapper
from systems.s4_combined.engine import S4Engine

SYSTEMS = ["s1", "s2", "s3", "s4"]
MAX_HISTORY = 3000
MAX_INCIDENTS = 200


def _session_only() -> bool:
    return bool(CONFIG.get("ibkr", {}).get("session_only", True))


def in_cash_session(now: datetime) -> bool:
    """Is the US cash equity market open at `now`? (E40)

    9:30-16:00 America/New_York, weekdays. zoneinfo handles EDT/EST so the
    UTC boundary moves correctly twice a year. Market holidays are NOT
    modelled: on one, orders simply do not fill and the next session's
    cancel-then-plan (E37) clears them - a wasted plan, never a wrong one.
    """
    from zoneinfo import ZoneInfo
    et = now.astimezone(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return False
    minutes = et.hour * 60 + et.minute
    return (9 * 60 + 30) <= minutes < (16 * 60)


def s5_enabled() -> bool:
    return bool(CONFIG.get("systems", {}).get("s5_event", {}).get("enabled", False))


# config key per book, so `enabled` means the same thing for all five
_BOOK_KEY = {"s1": "s1_quant", "s2": "s2_news", "s3": "s3_llm",
             "s4": "s4_combined", "s5": "s5_event"}


def _trading_days_only(s: "pd.Series") -> "pd.Series":
    """Drop weekends from a date-keyed daily series (E48e).

    The pre-registered decision rule counts TRADING days. Weekend rows exist
    because crypto reprices while the 80% equity sleeve cannot, so they are not
    days on which the two books can meaningfully disagree.
    """
    if s.empty:
        return s
    return s[pd.to_datetime(s.index).weekday < 5]


def book_enabled(book: str) -> bool:
    """E47: `enabled` was a kill switch that failed open.

    SYSTEMS was hardcoded, so S1-S4's flags were never read. config.yaml said
    s2/s3/s4 were disabled and carried a comment admitting the flags were
    stale, while all four books traded every tick. An operator reaching for the
    flag during an incident would have believed a book was stopped when it was
    still sending orders. A comment is not a control.

    Disabled means flat, which is the same thing the halt path does: the book
    is still marked and still keeps its history, it just stops taking risk.
    Defaults to True so a book missing from config keeps trading rather than
    silently vanishing.
    """
    cfg = CONFIG.get("systems", {}).get(_BOOK_KEY.get(book, book), {})
    return bool(cfg.get("enabled", True))

# Pre-registered no-trade thresholds (W2): below these, the tick marks equity
# but does NOT rebalance - a partial data fetch must never generate turnover.
MIN_EQUITY_COVERAGE = 0.90
MIN_CRYPTO_COVERAGE = 0.75

# Pessimistic fallbacks for the turnover cost surface when a name has no
# stamped ADV/vol inputs yet (first tick after deploy, or a symbol that
# entered a book before its inputs were stored): assume the universe ADV
# floor and a typical daily vol, so the missing-data path still charges
# impact instead of flattering the fill.
FALLBACK_ADV = 50e6
FALLBACK_DVOL = 0.02


@dataclass
class Coverage:
    equity: float
    crypto: float
    spy_ok: bool
    vix_ok: bool

    @property
    def ok(self) -> bool:
        return (self.equity >= MIN_EQUITY_COVERAGE
                and self.crypto >= MIN_CRYPTO_COVERAGE
                and self.spy_ok and self.vix_ok)

    def describe(self) -> str:
        return (f"eq={self.equity:.0%} cx={self.crypto:.0%} "
                f"spy={'ok' if self.spy_ok else 'MISSING'} vix={'ok' if self.vix_ok else 'MISSING'}")


@dataclass
class LightData:
    """Cheap per-tick market snapshot: marking prices + the equity bar date."""
    bar_date: str                                  # latest completed trading day
    prices: dict[str, float] = field(default_factory=dict)   # eq + crypto marks
    eq_cov: float = 0.0
    cx_cov: float = 0.0
    spy_close: float | None = None

    @property
    def ok(self) -> bool:
        return (bool(self.bar_date) and self.eq_cov >= MIN_EQUITY_COVERAGE
                and self.cx_cov >= MIN_CRYPTO_COVERAGE)


def asset_class(sym: str) -> str:
    return "crypto" if "/" in sym else "equity"


class LiveRuntime:
    def __init__(self, state_path: str = "data/state.json"):
        self.state_path = Path(state_path)
        self.cache_path = Path("data/cache/hist_cache.pkl")
        self.nav0 = float(CONFIG.fund.paper_capital)
        from core.data.universe import SECTORS
        self.governor = RiskGovernor(
            max_gross=CONFIG.risk.max_gross,
            max_net=CONFIG.risk.get("max_net", 1.0),
            dd_gate_1=CONFIG.risk.dd_gate_1,
            dd_gate_2=CONFIG.risk.dd_gate_2, daily_loss_kill=CONFIG.risk.daily_loss_kill,
            var_limit_95=CONFIG.risk.var_limit_95,
            max_sector=CONFIG.risk.get("max_sector", 0.25), sectors=SECTORS)
        # single cost surface (E19 deferred item): live marking charges the
        # SAME spread + square-root impact + commission stack the paper
        # broker and backtests use, per asset class
        self.cost_models = {"equity": CostModel(CostParams(**CONFIG.costs.equities)),
                            "crypto": CostModel(CostParams(**CONFIG.costs.crypto))}
        self.state = self._load()
        # append-only scored-headline ledger (H1 stage 1) - lives next to
        # state.json, never pruned; written fail-safe in _ingest_news
        self.sent_ledger_path = self.state_path.parent / "sentiment_history.jsonl"
        # append-only realized-fills ledger (E23 calibration dataset) - the
        # state ring is capped at 300 rows for the dashboard; this file keeps
        # every fill forever. Written fail-safe in _merge_ibkr_state.
        self.fills_ledger_path = self.state_path.parent / "fills_history.jsonl"
        # append-only S3 decisions ledger (director wish 2026-07-26): the
        # briefing the PM saw, its raw pre-wrapper proposal, the wrapper's
        # violations, which brain decided, the final book, and the next bar's
        # forward returns - the S3-v2 dataset. Purely PASSIVE measurement: it
        # never touches PM inputs, budgets or weights. Written fail-safe in
        # _log_s3_decision / _backfill_s3_forward.
        self.s3_ledger_path = self.state_path.parent / "s3_decisions.jsonl"
        self._legacy_eq: dict | None = None   # yfinance fallback stash (no grouped API)
        self._last_cov: pd.DataFrame | None = None  # VaR check on held ticks too

    def _active_systems(self) -> list[str]:
        """The books marked this tick. S5 joins only when enabled (additive)."""
        return SYSTEMS + (["s5"] if s5_enabled() else [])

    def _ensure_s5_state(self) -> None:
        """Lazily add S5's state slots the first tick it is enabled (additive
        schema migration - never wipes existing history)."""
        if not s5_enabled():
            return
        self.state["systems"].setdefault("s5", {"equity": self.nav0, "weights": {}})
        self.state.setdefault("equity_history", {}).setdefault("s5", [])

    # --- state ---------------------------------------------------------
    def _load(self) -> dict:
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            state.setdefault("schema", 2)
            state.setdefault("data_incidents", [])
            return state
        return {
            "schema": 2,
            "nav0": self.nav0,
            "systems": {s: {"equity": self.nav0, "weights": {}} for s in SYSTEMS},
            "prev_prices": {},
            "equity_history": {s: [] for s in SYSTEMS},
            "data_incidents": [],
            "ticks": 0, "last_tick": None, "last_actions": [], "regime": {},
        }

    def _daily_closes(self, key: str) -> pd.Series:
        hist = self.state["equity_history"].get(key, [])
        by_day: dict[str, float] = {}
        for ts, val in hist:
            by_day[ts[:10]] = val
        return pd.Series(by_day, dtype=float)

    def _realized_vol(self) -> dict:
        """Is the fair race actually fair? Nothing was measuring it (E48e).

        Every book is normalized to the same EX-ANTE target vol, and the whole
        horse race rests on that making them comparable. Ex-ante is an estimate
        though, and the estimate is worst exactly where it matters: S3 holds ~18
        names carved out of a 500-name covariance, so shrinkage and estimation
        error hit it hardest.

        Measured 2026-08-01, and it is not a small effect. S3's target was
        7.54% (10% x regime 0.754) and it realized 8.60%. S1 targeted 10% and
        realized 8.27%. Both miss, in opposite directions, and they cancel to
        within 4% of each other. The race is currently fair by accident rather
        than by construction, and nobody could have known either way.

        Diagnostic only. No decision rule reads this, and it changes no sizing.
        It exists so the next person can see the tracking error instead of
        assuming it away.
        """
        out: dict[str, dict] = {}
        cs = self.state.get("clock_start")
        for book in self.state.get("equity_history", {}):
            s = self._daily_closes(book)
            if cs:
                s = s[s.index >= cs]
            r = _trading_days_only(s).pct_change().dropna()
            if len(r) < 10:
                continue
            realized = float(r.std() * (252 ** 0.5))
            target = CONFIG.risk.vol_target_annual
            if book == "s3":
                target *= float(self.state.get("regime", {}).get("risk_scale", 1.0))
            out[book] = {"realized_annual": round(realized, 4),
                         "target_annual": round(target, 4),
                         "ratio": round(realized / target, 3) if target else None,
                         "n": len(r)}
        return out

    def _paired_s3_vs_s1(self) -> dict:
        """Pre-registered experiment readout: S3-S1 daily edge (DM/NW test).

        Decision rule (registered, do not move): no superiority claim before
        60 trading days AND p<0.05. Only days from clock_start count (E7/E10).
        """
        from backtest.metrics import paired_test
        a_ser, b_ser = self._daily_closes("s3"), self._daily_closes("s1")
        cs = self.state.get("clock_start")
        if cs:
            a_ser, b_ser = a_ser[a_ser.index >= cs], b_ser[b_ser.index >= cs]
        # E48e: the rule says 60 TRADING days and this counted calendar days.
        # 6 of the first 21 were Saturdays and Sundays, when the 80% equity
        # sleeve cannot move and the difference between the books is only the
        # crypto sleeve repricing. Left alone, the pre-registered gate would
        # have opened after ~43 trading days instead of 60.
        #
        # Filter BEFORE differencing, so Monday's return spans Friday's close
        # and carries the whole weekend move exactly once, which is what a
        # daily equity return series is. (Exchange holidays are a small
        # residual: both books hold the same flat equity sleeve through them.)
        a_ser, b_ser = _trading_days_only(a_ser), _trading_days_only(b_ser)
        a = a_ser.pct_change().dropna()
        b = b_ser.pct_change().dropna()
        res = paired_test(a, b)
        out = {k: (None if isinstance(v, float) and (v != v) else round(v, 4))
               for k, v in res.items()}
        out["verdict_allowed"] = bool(res["n"] >= 60 and res["p_value"] == res["p_value"]
                                      and res["p_value"] < 0.05)
        return out

    # --- common-universe diagnostic (E19 backlog: paired common-universe test)
    def _common_daily(self, key: str) -> pd.Series:
        hist = self.state.get("common_idx_history", {}).get(key, [])
        by_day: dict[str, float] = {}
        for ts, val in hist:
            by_day[ts[:10]] = val
        return pd.Series(by_day, dtype=float)

    def _mark_common(self, sys_name: str, pw: pd.Series, prev_prices: dict,
                     prices: dict, now: datetime) -> None:
        """Accrue the common-universe return index for s1/s3.

        S1 carries a 20% crypto sleeve S3 structurally cannot hold (its
        briefing is equities-only), so the headline paired test mixes 'does
        frontier judgment add alpha?' with 'what did crypto do?'. This index
        marks ONLY the equity legs of both books, with the same held weights
        and the same marking prices the main loop uses - a paired difference
        on the universe the books actually share. Gross of turnover costs on
        BOTH sides identically (fair race intact); additive state keys only.
        """
        if sys_name not in ("s1", "s3"):
            return
        realized = 0.0
        for sym, w in pw.items():
            if "/" in sym:                      # crypto excluded by design
                continue
            p0, p1 = prev_prices.get(sym), prices.get(sym)
            if p0 and p1 and p0 > 0:
                realized += float(w) * (p1 / p0 - 1.0)
        ci = self.state.setdefault("common_idx", {})
        ci[sys_name] = ci.get(sys_name, 1.0) * (1.0 + realized)
        hist = self.state.setdefault("common_idx_history", {})
        hist.setdefault(sys_name, []).append(
            [now.isoformat(), round(ci[sys_name], 8)])
        hist[sys_name] = hist[sys_name][-MAX_HISTORY:]

    def _paired_common(self) -> dict:
        """DIAGNOSTIC common-universe S3-S1 readout - NOT a decision rule.

        Same DM/Newey-West machinery and clock_start window as the
        pre-registered headline test, restricted to the shared equity
        universe. The verdict rule stays the FULL-book test above,
        unchanged; this dict deliberately carries NO verdict field so it
        can never gate a claim (post-hoc rule changes are forbidden).
        """
        from backtest.metrics import paired_test
        a_ser, b_ser = self._common_daily("s3"), self._common_daily("s1")
        cs = self.state.get("clock_start")
        if cs:
            a_ser, b_ser = a_ser[a_ser.index >= cs], b_ser[b_ser.index >= cs]
        # same trading-day basis as the headline test (E48e), so the diagnostic
        # and the decision rule cannot drift apart on how they count a day
        a_ser, b_ser = _trading_days_only(a_ser), _trading_days_only(b_ser)
        a = a_ser.pct_change().dropna()
        b = b_ser.pct_change().dropna()
        res = paired_test(a, b)
        out = {k: (None if isinstance(v, float) and (v != v) else round(v, 4))
               for k, v in res.items()}
        out["diagnostic_only"] = True
        return out

    def _incident(self, kind: str, detail: str, now: datetime) -> None:
        self.state["data_incidents"].append(
            {"ts": now.isoformat(), "kind": kind, "detail": detail})
        self.state["data_incidents"] = self.state["data_incidents"][-MAX_INCIDENTS:]
        # daily digest only - no per-tick Telegram spam
        queue_note(self.state, f"{kind}: {detail}")

    def _save(self) -> None:
        # atomic: a crash mid-write must never corrupt the fund's only ledger
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        os.replace(tmp, self.state_path)

    def _sync_halt_clear_from_disk(self) -> None:
        """Honor a human clear written by scripts/clear_halt.py.

        The runtime keeps state in memory and rewrites state.json every tick.
        Without this sync, clear_halt's disk edit is overwritten on the next
        save and the latch never actually lifts while the process is alive.
        """
        if "halt_latched" not in self.state or not self.state_path.exists():
            return
        try:
            on_disk = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if "halt_latched" not in on_disk:
            cleared = self.state.pop("halt_latched")
            send_telegram(
                f"✅ halt clear observed (was: {cleared.get('reason', '?')})",
                urgent=True)

    # --- LLM spend guard (hard daily caps) --------------------------------
    def _llm_budget_ok(self, kind: str) -> bool:
        llm_cfg = CONFIG.get("llm", {})
        caps = {"pm": llm_cfg.get("max_pm_calls_per_day", 12),
                "scorer": llm_cfg.get("max_scorer_calls_per_day", 400)}
        today = datetime.now(timezone.utc).date().isoformat()
        b = self.state.setdefault("llm_budget", {})
        if b.get("date") != today:
            b.update({"date": today, "pm": 0, "scorer": 0})
        b["pm_cap"], b["scorer_cap"] = caps["pm"], caps["scorer"]  # dashboard reads these, not a hardcoded guess
        return b.get(kind, 0) < caps[kind]

    def _llm_budget_spend(self, kind: str, n: int = 1) -> None:
        self.state.setdefault("llm_budget", {})[kind] = (
            self.state["llm_budget"].get(kind, 0) + n)

    # --- turnover cost (single cost surface) --------------------------------
    def _turnover_cost(self, pw: pd.Series, new_w: pd.Series,
                       equity: float) -> float:
        """Fractional cost of moving pw -> new_w on the shared cost surface.

        Live marking previously charged spread+commission only - market
        impact was free live, flattering paper Sharpe vs the backtest/broker
        CostModel stack (flagged in E19; the 'single cost surface' item).
        Per-name inputs (20d ADV, 30d daily vol) are stamped at each
        rebalance; names without inputs get pessimistic fallbacks so costs
        only ever err on the expensive side.
        """
        allk = pw.index.union(new_w.index)
        if len(allk) == 0:
            return 0.0
        turn = (new_w.reindex(allk).fillna(0.0)
                - pw.reindex(allk).fillna(0.0)).abs()
        inputs = self.state.get("cost_inputs", {})
        total = 0.0
        for sym in allk:
            t = float(turn[sym])
            if t <= 1e-12:
                continue
            adv, dvol = inputs.get(sym, (0.0, 0.0))
            adv = adv if adv and adv > 0 else FALLBACK_ADV
            dvol = dvol if dvol and dvol > 0 else FALLBACK_DVOL
            bd = self.cost_models[asset_class(sym)].estimate(
                t * max(equity, 0.0), adv=adv, daily_vol=dvol)
            total += t * (bd.slippage + bd.commission)
        return total

    # --- data: light tier ---------------------------------------------------
    def _fetch_light(self) -> LightData:
        """Marking prices + bar date, at minimal API cost.

        Polygon: ONE grouped-daily call for the whole equity market.
        yfinance fallback (no key): legacy full fetch, stashed for heavy reuse.
        Crypto: 8 ticker calls. Any failure degrades coverage; guard handles it.
        """
        eq_syms = [a.symbol for a in EQUITY_UNIVERSE]
        prices: dict[str, float] = {}
        bar_date, eq_cov, spy_close = "", 0.0, None

        provider = make_equity_provider()
        if hasattr(provider, "latest_grouped"):
            try:
                bar_date, bars = provider.latest_grouped()
            except Exception:
                bars = {}
            for s in eq_syms:
                b = bars.get(s)
                if b and b.get("close"):
                    prices[s] = float(b["close"])
            eq_cov = len(prices) / max(len(eq_syms), 1)
            spy_bar = bars.get("SPY")
            spy_close = float(spy_bar["close"]) if spy_bar and spy_bar.get("close") else None
        else:
            # legacy single-tier path (free fallback / small universe)
            try:
                eq = provider.history(eq_syms + ["SPY"], period="2y")
            except Exception:
                eq = {}
            self._legacy_eq = eq
            got = 0
            for s in eq_syms:
                df = eq.get(s)
                if df is not None and not df.empty:
                    prices[s] = float(df["close"].iloc[-1])
                    got += 1
            eq_cov = got / max(len(eq_syms), 1)
            dates = [d.index.max() for s, d in eq.items() if s != "SPY" and len(d)]
            bar_date = str(max(dates).date()) if dates else ""
            spy_df = eq.get("SPY")
            spy_close = float(spy_df["close"].iloc[-1]) if spy_df is not None and len(spy_df) else None

        try:
            cx = CryptoDataProvider(exchange=CONFIG.data.crypto_exchange).last_prices(
                [a.symbol for a in CRYPTO_UNIVERSE])
        except Exception:
            cx = {}
        prices.update(cx)
        cx_cov = len(cx) / max(len(CRYPTO_UNIVERSE), 1)

        return LightData(bar_date=bar_date, prices=prices, eq_cov=eq_cov,
                         cx_cov=cx_cov, spy_close=spy_close)

    # --- data: heavy tier ----------------------------------------------------
    def _fetch_heavy(self):
        """Full histories for signal computation (rebalance path only)."""
        if self._legacy_eq is not None:      # yfinance path already fetched it
            eq, self._legacy_eq = self._legacy_eq, None
        else:
            try:
                eq = make_equity_provider().history(
                    [a.symbol for a in EQUITY_UNIVERSE] + ["SPY"], period="2y")
            except Exception:
                eq = {}
        try:
            cx = CryptoDataProvider(exchange=CONFIG.data.crypto_exchange).history(
                [a.symbol for a in CRYPTO_UNIVERSE], timeframe="1d", limit=500)
        except Exception:
            cx = {}
        try:
            vix = MacroDataProvider().vix()
        except Exception:
            vix = pd.Series(dtype=float)

        cov = self._coverage(eq, cx, vix)
        if cov.ok:
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.cache_path, "wb") as fh:
                    pickle.dump((eq, cx, vix), fh)
            except Exception:
                pass
            return eq, cx, vix, False
        if self.cache_path.exists():
            try:
                with open(self.cache_path, "rb") as fh:
                    ceq, ccx, cvix = pickle.load(fh)
                return ceq, ccx, cvix, True
            except Exception:
                pass
        return eq, cx, vix, False

    @staticmethod
    def _coverage(eq: dict, cx: dict, vix: pd.Series) -> Coverage:
        eq_syms = [a.symbol for a in EQUITY_UNIVERSE]
        cx_syms = [a.symbol for a in CRYPTO_UNIVERSE]
        return Coverage(
            equity=sum(1 for s in eq_syms if s in eq and not eq[s].empty) / len(eq_syms),
            crypto=sum(1 for s in cx_syms if s in cx and not cx[s].empty) / len(cx_syms),
            spy_ok=("SPY" in eq and not eq["SPY"].empty),
            vix_ok=(vix is not None and len(vix) > 0),
        )

    # --- S2 news pipeline ------------------------------------------------------
    def _ingest_news(self, syms: list[str], now: datetime) -> None:
        """Fetch + score + store fresh headlines (no price data needed).

        Runs EVERY tick so information accumulates hourly even while books
        hold. RSS covers the liquid core (top-ADV subset); EDGAR 8-Ks cover
        the FULL universe every 6th tick.
        """
        from systems.s2_news.engine import NewsEngine
        from systems.s2_news.feeds import fetch_edgar_8k_items, fetch_rss_headlines, item_id
        from systems.s2_news.sentiment import OllamaScorer, TieredScorer

        rss_names = [s for s in rss_subset() if s in set(syms)] or syms
        items = fetch_rss_headlines(rss_names, per_symbol_limit=8)
        if self.state.get("ticks", 0) % 6 == 0:
            items += fetch_edgar_8k_items(syms, limit_per_symbol=2)

        seen = set(self.state.setdefault("s2_seen", []))
        fresh = [it for it in items if item_id(it) not in seen]
        if not fresh:
            return

        llm_cfg = CONFIG.get("llm", {})
        local = (OllamaScorer(model=llm_cfg.get("model", "qwen2.5:3b-instruct"),
                              host=llm_cfg.get("host", "http://localhost:11434"))
                 if llm_cfg.get("enabled") else None)
        use_anthropic = has_key("ANTHROPIC_API_KEY") and self._llm_budget_ok("scorer")
        if use_anthropic:
            from systems.s2_news.sentiment import AnthropicScorer
            llm = AnthropicScorer(model=llm_cfg.get("s2_model", "claude-haiku-4-5"),
                                  fallback=local)
        else:
            llm = local
        scorer = TieredScorer(llm_scorer=llm)
        NewsEngine(scorer=scorer).generate(fresh, universe=syms, now=now)
        if use_anthropic and scorer.llm_calls:
            self._llm_budget_spend("scorer", scorer.llm_calls)

        stored = self.state.setdefault("s2_scored", [])
        uni = {s.upper() for s in syms}
        for it in fresh:
            weight = it.confidence * it.materiality
            if weight > 0 and it.score != 0:
                for sym in it.symbols:
                    if sym.upper() in uni:
                        stored.append([it.timestamp.isoformat(), sym.upper(),
                                       round(it.score, 4), round(weight, 4)])
        # persistent sentiment ledger (director wish 2026-07-19, H1 stage 1):
        # the s2_scored buffer above is pruned to 7 days / 2000 rows every
        # tick, so the sentiment-conditioned event study (H1) has no history
        # to condition on. Append EVERY fresh universe-tagged headline
        # including neutral ones (weight 0 = 'no tone', itself data) - to an
        # append-only JSONL that outlives state.json. Fail-safe: a write
        # failure must never take the tick down.
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.sent_ledger_path, "a") as fh:
                for it in fresh:
                    for sym in it.symbols:
                        if sym.upper() in uni:
                            fh.write(json.dumps(
                                [it.timestamp.isoformat(), sym.upper(),
                                 round(it.score, 4),
                                 round(it.confidence * it.materiality, 4),
                                 it.source]) + "\n")
        except OSError:
            pass
        self.state["s2_news_processed"] = (
            self.state.get("s2_news_processed", 0) + len(fresh))
        seen |= {item_id(it) for it in items}
        self.state["s2_seen"] = list(seen)[-8000:]

    def _news_raw(self, now: datetime) -> pd.Series:
        """Decayed sentiment aggregate from stored scores (pure state, no I/O)."""
        from systems.s2_news.events import time_decay_aggregate
        cutoff = now - timedelta(days=7)
        stored = [t for t in self.state.get("s2_scored", [])
                  if datetime.fromisoformat(t[0]) >= cutoff][-2000:]
        self.state["s2_scored"] = stored
        tuples = [(datetime.fromisoformat(a), b, c, d) for a, b, c, d in stored]
        raw = time_decay_aggregate(tuples, now=now, tau_hours=48.0)
        return raw[raw.abs() > 1e-6]

    def _build_s2_book(self, cov: pd.DataFrame, now: datetime) -> tuple[pd.Series, pd.Series]:
        from systems.s1_quant.signals import cross_sectional_zscore
        raw = self._news_raw(now)
        empty = pd.Series(dtype=float)
        if len(raw) < 3:
            return empty, empty
        z = cross_sectional_zscore(raw)
        w = scale_to_target_vol(
            z, cov, target_vol=CONFIG.risk.vol_target_annual,
            max_position=CONFIG.risk.max_position, max_gross=CONFIG.risk.max_gross)
        return w, z

    def _run_s2(self, eq_hist: dict, cov: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        """Compat wrapper (tests): ingest for the given names, then build."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        self._ingest_news(list(eq_hist.keys()), now)
        return self._build_s2_book(cov, now)

    # --- decision gate -----------------------------------------------------------
    def _decision_fingerprint(self, bar_date: str, s2_raw: pd.Series) -> str:
        """Legacy quantized fingerprint (kept for tests); live gating uses
        _should_rebalance's reference-vector materiality test."""
        import hashlib
        q = CONFIG.get("rebalance", {}).get("s2_z_round", 0.25)
        if len(s2_raw):
            rounded = {str(k): round(round(float(v) / q) * q, 4)
                       for k, v in sorted(s2_raw.items())}
            s2_part = json.dumps(rounded, sort_keys=True)
        else:
            s2_part = "none"
        return (bar_date or "none") + "|" + hashlib.md5(s2_part.encode()).hexdigest()[:12]

    def _should_rebalance(self, bar_date: str, s2_raw: pd.Series) -> tuple[bool, str]:
        """Materiality gate. Rebalance only on:
        - a NEW daily bar, or
        - a real news shock: some name's decayed score moved >= threshold
          versus the snapshot taken AT THE LAST REBALANCE.

        The earlier quantum-crossing fingerprint failed at 501 names: with
        ~1000 live scored items, decay pushed SOME name across a 0.20 grid
        line almost every hour, so the gate never held - Claude budget burned
        by noon and decisions degraded to the heuristic (E15 defect).
        Comparing against a fixed reference vector is monotone in real change:
        pure decay drifts a score by <0.1/h and cannot trigger; a fresh
        material headline jumps it by >=0.5 at once.
        """
        thr = CONFIG.get("rebalance", {}).get("news_shock_threshold", 0.5)
        ref = self.state.get("rebalance_ref") or {}
        if bar_date and bar_date != ref.get("bar_date"):
            return True, f"new bar {bar_date}"
        ref_scores = ref.get("news", {})
        cur = {str(k): float(v) for k, v in s2_raw.items()}
        for name in set(cur) | set(ref_scores):
            if abs(cur.get(name, 0.0) - ref_scores.get(name, 0.0)) >= thr:
                return True, f"news shock {name}"
        return False, ""

    def _stamp_rebalance_ref(self, bar_date: str, s2_raw: pd.Series) -> None:
        self.state["rebalance_ref"] = {
            "bar_date": bar_date,
            "news": {str(k): round(float(v), 4) for k, v in s2_raw.items()},
        }

    # --- S5 event sleeve (forward shadow of the confirmed 8k-drift edge) -------
    def _run_s5(self, eq: dict, cov: pd.DataFrame, bar_date: str,
                now: datetime) -> pd.Series:
        """Build S5's target book from live 8-K events. Reuses the exact
        windowing/sign logic of the confirmed calendar_time_daily backtest.

        EDGAR is polled at most once per calendar day; the reaction sign is
        derived from the price panel the heavy path already fetched. Any
        failure degrades to holding the current S5 book (never a wrong trade).
        """
        from systems.s5_event.engine import S5EventEngine
        from systems.s5_event.feed import refresh_raw_filings, signed_events
        s5_cfg = CONFIG.get("s5", {})
        try:
            from research.h1_event_study import fetch_8k_events
            eq_syms = [a.symbol for a in EQUITY_UNIVERSE][:int(s5_cfg.get("n_names", 500))]
            spy_ret = eq["SPY"]["close"].pct_change().dropna()
            cached = self.state.get("s5_raw_filings", [])
            rows, fetch_date = refresh_raw_filings(
                cached, eq_syms, pd.Timestamp(bar_date),
                exit_lag=int(s5_cfg.get("exit_lag", 10)),
                last_fetch=self.state.get("s5_last_fetch"),
                fetch_fn=fetch_8k_events)
            self.state["s5_raw_filings"] = rows
            self.state["s5_last_fetch"] = fetch_date
            events = signed_events(rows, eq, spy_ret)
            eng = S5EventEngine(
                entry_lag=int(s5_cfg.get("entry_lag", 2)),
                exit_lag=int(s5_cfg.get("exit_lag", 10)),
                min_units=int(s5_cfg.get("min_units", 4)),
                target_vol=CONFIG.risk.vol_target_annual,
                max_position=CONFIG.risk.max_position,
                max_gross=CONFIG.risk.max_gross)
            res = eng.generate(events, today=bar_date, cov=cov)
            self.state["s5_stats"] = {"n_units": res.n_units, "n_names": res.n_names,
                                      "n_events": len(events)}
            return res.weights
        except Exception as exc:
            self._incident("S5-DEGRADED", f"{type(exc).__name__}: {exc}", now)
            return pd.Series(self.state["systems"].get("s5", {}).get("weights", {}), dtype=float)

    # --- S3 decisions ledger (S3-v2 dataset, director wish 2026-07-26) --------
    def _log_s3_decision(self, briefing: dict, s3_source: str, raw_proposal,
                         violations, final_w: pd.Series, bar_date: str,
                         decision_prices: dict, now: datetime) -> None:
        """Append one line per S3 decision (including 'held') to the ledger.

        S3's P&L cannot today be decomposed into selection vs sizing vs
        timing: the briefing the PM saw, its raw pre-wrapper proposal and the
        wrapper's violations are all discarded at the end of each rebalance
        and are NOT reconstructable later (headlines decay, the focus set is
        instantaneous, the raw proposal lives only in the API response). This
        is the S3-v2 dataset. Strictly PASSIVE: it reads objects the decision
        already produced and never touches PM inputs, budgets or weights.
        Written fail-safe - a write failure must never take the tick down.
        """
        import hashlib
        row = {
            "kind": "decision",
            "ts": now.isoformat(),
            "bar_date": bar_date,
            "briefing_hash": hashlib.md5(
                json.dumps(briefing, sort_keys=True, default=str).encode()
            ).hexdigest()[:16],
            "pm_source": s3_source,
            "regime": briefing.get("regime"),
            "briefing_names": briefing.get("names"),
            "proposal": raw_proposal,
            "violations": list(violations),
            "final_weights": {sym: round(float(w), 6)
                              for sym, w in final_w.items() if abs(w) > 1e-9},
            "decision_prices": {sym: round(float(p), 4)
                                for sym, p in decision_prices.items()},
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.s3_ledger_path, "a") as fh:
                # default=str: an exotic value inside a PM proposal must be
                # recorded-as-text, never raise inside the tick loop
                fh.write(json.dumps(row, default=str) + "\n")
        except OSError:
            pass
        # stamped unconditionally (outside the try): a disk hiccup must not
        # desync the forward leg from the decision it measures
        if decision_prices:
            self.state["s3_forward_pending"] = {
                "ts": row["ts"], "bar_date": bar_date,
                "prices": row["decision_prices"]}

    def _backfill_s3_forward(self, bar_date: str, prices: dict) -> None:
        """Append the NEXT bar's forward returns for the last logged decision.

        Fires once, on the first tick that rolls to a new bar; names without a
        fresh mark are skipped (a missing mark is not a zero return). Purely
        observational - it reads marks the tick already fetched and gates
        nothing. Fail-safe write; the pending stamp is cleared either way so a
        failed write cannot pin a stale decision forever.
        """
        pending = self.state.get("s3_forward_pending")
        if not pending or not bar_date or bar_date == pending.get("bar_date"):
            return
        returns = {sym: round(prices[sym] / p0 - 1.0, 6)
                   for sym, p0 in pending.get("prices", {}).items()
                   if p0 and prices.get(sym)}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.s3_ledger_path, "a") as fh:
                fh.write(json.dumps({
                    "kind": "forward",
                    "decision_ts": pending.get("ts"),
                    "decision_bar": pending.get("bar_date"),
                    "forward_bar": bar_date,
                    "returns": returns}) + "\n")
        except OSError:
            pass
        self.state.pop("s3_forward_pending", None)

    # --- full rebalance (heavy path) ------------------------------------------------
    def _run_systems(self, eq, cx, vix, now: datetime):
        eq_hist = {s: eq[s] for s in eq if s != "SPY"}
        spy = eq["SPY"]["close"]

        close, _ = build_panels(eq_hist)
        reg = compute_regime(close, spy, vix)
        merged = {**eq_hist, **(cx or {})}
        close_all = pd.DataFrame({s: d["close"] for s, d in merged.items()}).sort_index()
        cov = ledoit_wolf_cov(log_returns(close_all))

        # single cost surface: stamp per-name 20d ADV + 30d daily vol so the
        # hourly marking loop charges the SAME spread+impact+commission stack
        # the paper broker and backtests use (E19: live impact was a known gap)
        cost_inputs: dict[str, list[float]] = {}
        for s, df in merged.items():
            adv = float(average_dollar_volume(df))
            dv = df["close"].pct_change().tail(30).std()
            cost_inputs[s] = [round(adv, 2) if adv == adv else 0.0,
                              round(float(dv), 6) if dv == dv else 0.0]
        self.state["cost_inputs"] = cost_inputs

        w_s2, s2_z = self._build_s2_book(cov, now.replace(tzinfo=None))

        # S1: equities (0.8) + crypto (0.2) merged
        s1_eq = QuantEngine(target_vol=CONFIG.risk.vol_target_annual,
                            max_position=CONFIG.risk.max_position,
                            max_gross=CONFIG.risk.max_gross, market_neutral=True).generate(eq_hist)
        w_s1 = s1_eq.weights * CONFIG.allocation.equities
        if cx:
            s1_cx = QuantEngine(target_vol=CONFIG.risk.vol_target_annual,
                                max_position=CONFIG.risk.max_position,
                                max_gross=CONFIG.risk.max_gross, market_neutral=False).generate(cx)
            w_s1 = w_s1.add(s1_cx.weights * CONFIG.allocation.crypto, fill_value=0.0)
        # fair race: the 80/20 merge changes ex-ante vol, so the merged S1 book
        # goes through the SAME final vol-normalization every other book gets
        w_s1 = scale_to_target_vol(
            w_s1, cov, target_vol=CONFIG.risk.vol_target_annual,
            max_position=CONFIG.risk.max_position, max_gross=CONFIG.risk.max_gross)

        s2_scores = s2_z.reindex(s1_eq.combined_score.index).fillna(0.0)

        # A5: focused briefing - 500 names would be ~20k tokens of noise; the PM
        # sees its strongest quant signals, live news names, and its own book.
        top_k = CONFIG.get("rebalance", {}).get("briefing_top_k", 40)
        focus = set(s1_eq.combined_score.abs().nlargest(top_k).index)
        focus |= set(s2_scores[s2_scores.abs() >= 0.5].index)
        focus |= set(self.state["systems"]["s3"].get("weights", {}))
        focus = [s for s in focus if s in eq_hist][:60]

        briefing = build_briefing(focus, {s: eq_hist[s] for s in focus},
                                  s1_eq.combined_score, s2_scores, s1_eq.vols, reg)
        adv = {s: average_dollar_volume(eq_hist[s]) for s in focus}
        wrapper = RiskWrapper(universe={s.upper() for s in focus}, nav=self.nav0,
                              max_position=CONFIG.risk.max_position,
                              max_gross=CONFIG.risk.max_gross,
                              adv_cap=CONFIG.risk.liquidity_adv_cap)

        # bar this decision was made on - also reused by the S5 branch below
        bar_date = str(eq["SPY"]["close"].index.max().date())

        llm_cfg = CONFIG.get("llm", {})
        if has_key("ANTHROPIC_API_KEY") and not self._llm_budget_ok("pm"):
            # PM budget exhausted this tick. HOLD S3's existing book rather than
            # let a weaker brain decide - mixing brains inside the final-config
            # window contaminates the Opus-vs-quant attribution (E15b). A held
            # day is a legitimate "no new decision", not a different decision.
            w_s3 = pd.Series(self.state["systems"]["s3"].get("weights", {}), dtype=float)
            s3_source = "held"
            decision_prices = {s: float(eq_hist[s]["close"].iloc[-1])
                               for s in set(focus) | set(w_s3.index) if s in eq_hist}
            self._log_s3_decision(briefing, "held", None, [], w_s3, bar_date,
                                  decision_prices, now)
        else:
            if llm_cfg.get("enabled"):
                base_pm = OllamaPM(model=llm_cfg.get("model", "qwen2.5:3b-instruct"),
                                   host=llm_cfg.get("host", "http://localhost:11434"),
                                   max_position=CONFIG.risk.max_position)
            else:
                base_pm = HeuristicPM(CONFIG.risk.max_position)
            if has_key("ANTHROPIC_API_KEY"):
                pm = AnthropicPM(model=llm_cfg.get("s3_model", "claude-opus-4-8"),
                                 max_position=CONFIG.risk.max_position, fallback=base_pm)
            else:
                pm = base_pm
            s3 = S3Engine(wrapper=wrapper, pm=pm).generate(briefing, adv=adv)
            s3_source = s3.pm_source
            if s3.pm_source == "anthropic":
                self._llm_budget_spend("pm")
            # E49: no regime multiplier here. S3 targets the same 10% as every
            # other book, so the pre-registered S3-vs-S1 test measures decision
            # quality and nothing else. The throttle used to make the treatment
            # arm differ from the control in TWO ways at once, and a difference
            # you cannot attribute is not evidence.
            #
            # S3 still SEES the regime: `reg` goes into the briefing below, so
            # if regime timing has value the PM can express it by choosing
            # different positions, which is exactly what we are testing. What
            # is gone is the code placing a timing bet on the PM's behalf.
            #
            # Regime gating itself is H6, an untested hypothesis in this
            # project's own queue. It belongs in the harness with a bar and a
            # window, not baked live into the arm being measured.
            w_s3 = scale_to_target_vol(
                s3.weights, cov,
                target_vol=CONFIG.risk.vol_target_annual,
                max_position=CONFIG.risk.max_position,
                max_gross=CONFIG.risk.max_gross,
            )
            decision_prices = {s: float(eq_hist[s]["close"].iloc[-1])
                               for s in set(focus) | set(w_s3.index) if s in eq_hist}
            self._log_s3_decision(briefing, s3_source, s3.raw_proposal,
                                  s3.violations, w_s3, bar_date,
                                  decision_prices, now)

        books = {"s1": w_s1, "s3": w_s3}
        if not w_s2.empty:
            books["s2"] = w_s2
        # feed realized per-book returns so "risk_parity" is actually risk
        # parity live (previously system_returns=None silently degraded the
        # combiner to equal-weight); needs >=20 daily marks per book
        sys_rets = {}
        for k in books:
            r = self._daily_closes(k).pct_change().dropna().tail(60)
            if len(r) >= 20:
                sys_rets[k] = r
        s4 = S4Engine(method="risk_parity").generate(
            books, system_returns=sys_rets if len(sys_rets) == len(books) else None)
        w_s4 = scale_to_target_vol(
            s4.weights, cov, target_vol=CONFIG.risk.vol_target_annual,
            max_position=CONFIG.risk.max_position, max_gross=CONFIG.risk.max_gross)
        weights = {"s1": w_s1, "s2": w_s2, "s3": w_s3, "s4": w_s4}
        # S5 is a standalone forward shadow - NOT part of the S4 ensemble.
        # Gated: when disabled this branch never runs and the four-book race is
        # byte-for-byte unchanged.
        if s5_enabled():
            weights["s5"] = self._run_s5(eq, cov, bar_date, now)
        # A book whose kill switch is off goes flat rather than disappearing:
        # it keeps being marked, so its equity history stays intact and the
        # paired book comparison does not get a hole in it.
        for book, w in weights.items():
            if not book_enabled(book):
                weights[book] = w * 0.0
        return weights, reg.as_dict(), cov, s3_source

    # --- IBKR paper-execution mirror (E24) ---------------------------------------
    def _ibkr_target_weights(self, w_s4: pd.Series | None = None) -> dict[str, float]:
        if w_s4 is not None:
            return {str(s): float(v) for s, v in w_s4.items()}
        if self.state.get("halt_latched"):
            return {}
        return dict(self.state["systems"]["s4"].get("weights", {}))

    _FILLS_SEEN_TAIL = 20_000        # ~5 busy sessions; reqExecutions is day-scoped

    def _fills_seen(self) -> set[str]:
        """execIds already persisted, seeded once from the ledger's tail (E36).

        In memory rather than in state.json: the set is derivable from the
        ledger (the source of truth), so persisting it would grow every backup
        and the quant view for no gain. Seeding from the tail keeps it bounded
        while comfortably covering the only window the broker can re-report
        IBKR replays the current day and nothing earlier.
        """
        cached = getattr(self, "_fills_seen_cache", None)
        if cached is not None:
            return cached
        seen: set[str] = set()
        try:
            from collections import deque
            with open(self.fills_ledger_path) as fh:
                for line in deque(fh, maxlen=self._FILLS_SEEN_TAIL):
                    try:
                        fid = json.loads(line).get("id")
                    except json.JSONDecodeError:
                        continue
                    if fid:
                        seen.add(fid)
        except OSError:
            pass
        seen.update(f["id"] for f in (self.state.get("ibkr", {}).get("fills") or [])
                    if f.get("id"))
        self._fills_seen_cache = seen
        return seen

    def _merge_ibkr_state(self, report: dict) -> None:
        """Persist broker snapshot into state for the dashboard."""
        blk = self.state.setdefault("ibkr", {})
        # Dedupe against the LEDGER, not the 300-row display ring (E36). The
        # ring holds ~1h of a busy session, so a ring-scoped `seen` silently
        # re-admitted evicted fills and - once the broker began reporting the
        # whole day - would have appended thousands of duplicates per read.
        seen = self._fills_seen()
        new_fills = [f for f in report.pop("fills_today", [])
                     if f.get("id") and f["id"] not in seen]
        seen.update(f["id"] for f in new_fills)
        fills = blk.get("fills", []) + new_fills
        # append-only fills ledger (E23 calibration dataset): the ring below
        # prunes to 300 rows and the broker report used to truncate at 25 per
        # read, so realized-fill history was being destroyed faster than the
        # cost-surface decision could accumulate it - same defect class as
        # the sentiment ledger (P0009), and equally unbackfillable (IBKR only
        # replays the current day). Persist EVERY new fill. Fail-safe: a
        # write failure must never take the tick down. Consumers dedupe by
        # execId (eviction from the ring can re-admit a same-day fill).
        if new_fills:
            try:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.fills_ledger_path, "a") as fh:
                    for f in new_fills:
                        fh.write(json.dumps(f) + "\n")
            except OSError:
                pass
        is_refresh = report.get("mode") == "refresh"
        if is_refresh:
            preserved = {k: blk[k] for k in (
                "last_sync", "orders_placed", "order_preview", "plan", "failed",
            ) if k in blk}
            prev_mode = blk.get("mode")
            blk.update(report)
            blk.update(preserved)
            # keep operational mode as last execute/dry_run when refreshing
            if prev_mode in ("execute", "dry_run"):
                blk["mode"] = prev_mode
        else:
            blk.update(report)
        blk["fills"] = fills[-300:]

    def _mirror_to_ibkr(self, w_s4: pd.Series, prices: dict, now: datetime) -> None:
        """Mirror S4's liquid equity core onto the IBKR paper account.

        Weights are dimensionless, so they apply to IBKR's own NAV (~$1M);
        the internal $3M books are untouched. Called with FINAL weights only
        (post-governor, post-halt), so a latched halt arrives as an all-zero
        book and flattens the IBKR account too.
        """
        from core.broker.ibkr_broker import IBKRBroker
        cfg = dict(CONFIG.get("ibkr", {}))
        # rotating clientId (base+11 .. base+18): E27 gave the refresh path a
        # rotating id but the mirror kept the FIXED base id - after the 07-23
        # 19:16 mirror timeout abandoned a thread still holding that id, every
        # hourly rebalance sync collided on "clientId already in use" and timed
        # out (SYNC-FAILs 20:19/21:20/23:24) until the Gateway's daily restart.
        # Band is disjoint from refresh's base+1..+8 so the two paths can never
        # collide with each other either.
        cfg["client_id"] = int(cfg.get("client_id", 7)) + 11 + (self.state.get("ticks", 0) % 8)
        broker = IBKRBroker(cfg)
        report = broker.sync(
            self._ibkr_target_weights(w_s4), prices,
            execute=bool(cfg.get("execute", False)),
            max_position=CONFIG.risk.max_position,
            max_gross=CONFIG.risk.max_gross)
        self._merge_ibkr_state(report)
        if report.get("orders_placed") or report.get("failed"):
            queue_note(
                self.state,
                f"IBKR mirror: {report.get('orders_placed', 0)} orders "
                f"({report.get('mode')}), nav=${report.get('nav', 0):,.0f}, "
                f"failed={len(report.get('failed', []))}")

    def _ibkr_bounded(self, fn, timeout: float) -> dict:
        """Run an IBKR broker call in a daemon thread and ABANDON it if it
        exceeds `timeout`. A broker read/order must never be able to freeze
        the fund's core tick loop - ib_async's connect has a timeout but its
        post-connect ops (accountSummary/positions/reqExecutions/placeOrder)
        do not, so a Gateway that goes unresponsive mid-operation blocks the
        socket forever (observed 2026-07-22: tick loop hung ~8h on ep_poll).
        The worker gets its own event loop; on timeout the thread is left to
        die with its socket and we move on.
        """
        result: dict = {}

        def run():
            import asyncio
            asyncio.set_event_loop(asyncio.new_event_loop())
            try:
                fn()
                result["ok"] = True
            except Exception as exc:
                result["err"] = f"{type(exc).__name__}: {exc}"

        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            result["timeout"] = True
        return result

    def _refresh_ibkr(self, prices: dict, now: datetime) -> None:
        """Read-only IBKR snapshot every tick so the dashboard matches the account.

        Fail-QUIET: a read failure (Gateway mid-restart, transient socket) is an
        expected momentary state, not a fund-integrity event - it records a
        marker for the dashboard and returns, never raising and never appending
        a data_incident (which would spam every tick the Gateway is down and
        push real incidents out of the ring buffer).
        """
        from core.broker.ibkr_broker import IBKRBroker
        cfg = dict(CONFIG.get("ibkr", {}))
        # rotating clientId (base+1 .. base+8): if a prior refresh was abandoned
        # on a hung socket, the next attempt uses a fresh id and can still
        # connect instead of colliding on "clientId already in use"
        cfg["client_id"] = int(cfg.get("client_id", 7)) + 1 + (self.state.get("ticks", 0) % 8)
        broker = IBKRBroker(cfg)
        try:
            report = broker.refresh(
                self._ibkr_target_weights(), prices,
                max_position=CONFIG.risk.max_position)
        except Exception as exc:
            blk = self.state.setdefault("ibkr", {})
            blk["refresh_error"] = f"{type(exc).__name__}: {exc}"
            blk["refresh_error_ts"] = now.isoformat()
            return
        self.state.setdefault("ibkr", {}).pop("refresh_error", None)
        self._merge_ibkr_state(report)

    # --- one tick ---------------------------------------------------------------
    def tick(self) -> dict:
        now = datetime.now(timezone.utc)
        t0 = time.time()
        self._legacy_eq = None
        # pick up human halt-clears before any latch / save logic this tick
        self._sync_halt_clear_from_disk()
        self._ensure_s5_state()
        active = self._active_systems()
        light = self._fetch_light()
        prices = light.prices

        prev_w = {k: pd.Series(self.state["systems"][k].get("weights", {}), dtype=float)
                  for k in active}

        no_trade = False
        rebalanced = False
        s3_source = None
        cov = None
        regime = self.state.get("regime", {})
        weights = prev_w

        if not light.ok:
            # degraded market snapshot: hold, mark with whatever we have
            no_trade = True
            self._incident("NO-TRADE tick",
                           f"light coverage eq={light.eq_cov:.0%} cx={light.cx_cov:.0%} "
                           f"bar={light.bar_date or 'MISSING'}", now)
        else:
            # news accumulates hourly regardless of whether books trade
            eq_syms = [a.symbol for a in EQUITY_UNIVERSE]
            try:
                self._ingest_news(eq_syms, now.replace(tzinfo=None))
            except Exception:
                pass
            raw = self._news_raw(now.replace(tzinfo=None))
            # forward leg of the S3 decisions ledger: the pending decision is
            # marked at THIS bar's prices BEFORE _run_systems can stamp a new
            # one (a tick can both roll the bar and rebalance)
            self._backfill_s3_forward(light.bar_date, prices)
            should, reason = self._should_rebalance(light.bar_date, raw)

            if should or not len(prev_w["s4"]):
                eq, cx, vix, from_cache = self._fetch_heavy()
                coverage = self._coverage(eq, cx, vix)
                if (not coverage.ok) or from_cache:
                    no_trade = True
                    self._incident(
                        "NO-TRADE tick",
                        f"heavy coverage {coverage.describe()}"
                        + (" (served from cache)" if from_cache else ""), now)
                else:
                    weights, regime, cov, s3_source = self._run_systems(eq, cx, vix, now)
                    self._stamp_rebalance_ref(light.bar_date, raw)
                    self.state["last_rebalance"] = now.isoformat()
                    self.state["last_rebalance_reason"] = reason or "bootstrap"
                    rebalanced = True

        # governor runs EVERY tick (gates must be able to halt a held book)
        hist_s4 = self.state["equity_history"]["s4"]
        if hist_s4:
            s4_eq_hist = pd.Series(
                [e for _, e in hist_s4] + [self.state["systems"]["s4"]["equity"]],
                index=pd.DatetimeIndex([pd.Timestamp(t) for t, _ in hist_s4] + [now]),
            )
        else:
            s4_eq_hist = pd.Series(dtype=float)
        if cov is not None:
            self._last_cov = cov
        decision = self.governor.assess(
            weights["s4"], equity_curve=s4_eq_hist,
            cov_daily=cov if cov is not None else self._last_cov)
        weights["s4"] = decision.weights
        actions = (["NO-TRADE: degraded data"] if no_trade else []) + decision.actions

        # halt LATCH: once tripped, ALL books stay flat until a human clears
        # via scripts/clear_halt.py (disk clear is honored next tick via
        # _sync_halt_clear_from_disk). Without the latch, DD recovery
        # auto-re-risked the very next tick.
        halt_just_latched = False
        if decision.halted and not self.state.get("halt_latched"):
            self.state["halt_latched"] = {"ts": now.isoformat(),
                                          "reason": "; ".join(decision.actions)}
            halt_just_latched = True
            send_telegram("🛑 HALT LATCHED - all books flat until human clear "
                          "(scripts/clear_halt.py)", urgent=True)
        if self.state.get("halt_latched"):
            for k in active:
                weights[k] = weights.get(k, pd.Series(dtype=float)) * 0.0
            actions.append(f"halt latched since {self.state['halt_latched']['ts']}")

        self.state["last_actions"] = actions
        if decision.halted or decision.risk_scale < 1.0:
            queue_note(self.state, "GOVERNOR: " + "; ".join(decision.actions))

        # --- marking: hold or trade, equity always marks to market -----------
        prev_prices = self.state.get("prev_prices", {})
        # borrow accrues once per calendar day on the short leg (was a free
        # short book before - paper Sharpe flattered vs any real broker).
        # Day-count basis matches CostModel.borrow_cost (/252 trading days).
        today = now.date().isoformat()
        charge_borrow = self.state.get("last_borrow_date") != today
        if charge_borrow:
            self.state["last_borrow_date"] = today
        borrow_daily = CONFIG.costs.equities.get("borrow_annual_bps", 50) * 1e-4 / 252.0

        # attribution accumulates across the trading day (reset on a new bar):
        # equities only re-mark once/day (two-tier architecture) while crypto
        # re-marks every tick, so a single-tick snapshot would show equities
        # at $0 on every tick except the bar-rollover one, dwarfed by whatever
        # crypto did in the meantime. Accumulating keeps the panel reconciled
        # with the "Day P&L" KPI regardless of which tick the dashboard is
        # viewed on.
        bar_key = light.bar_date or self.state.get("s4_attribution_bar")
        if bar_key and bar_key != self.state.get("s4_attribution_bar"):
            self.state["s4_attribution_bar"] = bar_key
            self.state["s4_attribution_acc"] = {}
        s4_acc = self.state.setdefault("s4_attribution_acc", {})

        missing_per_book = {s: 0.0 for s in active}
        s4_equity_before = self.state["systems"]["s4"]["equity"]
        for sys_name in active:
            sysd = self.state["systems"][sys_name]
            pw = pd.Series(sysd.get("weights", {}), dtype=float)
            new_w = weights.get(sys_name, pd.Series(dtype=float))
            equity_before = sysd["equity"]

            realized = 0.0
            if prev_prices:
                for sym, w in pw.items():
                    p0, p1 = prev_prices.get(sym), prices.get(sym)
                    if p0 and p1 and p0 > 0:
                        r = p1 / p0 - 1.0
                        realized += w * r
                        if sys_name == "s4":
                            entry = s4_acc.setdefault(sym, [0.0, 0.0])
                            entry[0] = float(w)
                            entry[1] += float(w) * r * equity_before
                    elif p0 and not p1:
                        missing_per_book[sys_name] += abs(float(w))
            # common-universe diagnostic index (equity legs of s1/s3 only)
            # feeds the s3_vs_s1_common readout; no effect on book equity
            self._mark_common(sys_name, pw, prev_prices, prices, now)
            # spread + impact + commission on turnover - the same CostModel
            # stack the paper broker and backtests charge (single cost surface)
            cost = self._turnover_cost(pw, new_w, equity_before)
            if charge_borrow:
                short_gross = float(pw[pw < 0].abs().sum())
                cost += short_gross * borrow_daily

            sysd["equity"] = sysd["equity"] * (1 + realized) * (1 - cost)
            sysd["weights"] = {k: float(v) for k, v in new_w.items() if abs(v) > 1e-9}
            self.state["equity_history"][sys_name].append(
                [now.isoformat(), round(sysd["equity"], 2)])
            self.state["equity_history"][sys_name] = \
                self.state["equity_history"][sys_name][-MAX_HISTORY:]

        # today's per-symbol $ P&L for the combined book, cumulative since the
        # last bar (see s4_acc above) - reconciles with the Day P&L KPI.
        s4_rows = []
        for sym, (w, cum_usd) in s4_acc.items():
            r_disp = cum_usd / (w * s4_equity_before) if abs(w) > 1e-12 else 0.0
            s4_rows.append([sym, round(w, 6), round(r_disp, 6), round(cum_usd, 2)])
        s4_rows.sort(key=lambda x: abs(x[3]), reverse=True)
        self.state["s4_attribution"] = s4_rows[:25]

        worst_missing = max(missing_per_book.values()) if missing_per_book else 0.0
        if worst_missing > 0.02:
            detail = ", ".join(f"{k}={v:.1%}" for k, v in missing_per_book.items() if v > 0)
            self._incident("STALE-MARK",
                           f"missing marks (>2% in a book): {detail}", now)

        # SPY benchmark (grouped bar carries SPY on the light path too)
        if light.spy_close:
            ref = self.state.setdefault("benchmark_ref", light.spy_close)
            bench = self.state.setdefault("benchmark", [])
            bench.append([now.isoformat(), round(self.nav0 * light.spy_close / ref, 2)])
            self.state["benchmark"] = bench[-MAX_HISTORY:]

        # experiment bookkeeping
        self.state["s3_vs_s1"] = self._paired_s3_vs_s1()
        self.state["s3_vs_s1_common"] = self._paired_common()
        self.state["realized_vol"] = self._realized_vol()
        self.state["data_provider"] = equity_provider_name()
        self.state["anthropic_active"] = has_key("ANTHROPIC_API_KEY")
        counts = self.state.setdefault("s3_pm_counts", {"ollama": 0, "heuristic": 0})
        if s3_source is not None:
            self.state["s3_pm_source"] = s3_source
            counts[s3_source] = counts.get(s3_source, 0) + 1
        if s3_source == "anthropic" and "clock_start" not in self.state:
            self.state["clock_start"] = now.date().isoformat()
            queue_note(
                self.state,
                f"60-day clock started {self.state['clock_start']}")
        self.state.setdefault("s3_pm_source", "heuristic")
        self.state.setdefault("s2_news_processed", 0)

        self.state.setdefault("prev_prices", {}).update(prices)
        self.state["ticks"] += 1
        self.state["last_tick"] = now.isoformat()
        self.state["regime"] = regime

        # IBKR paper-execution mirror (E24): S4's FINAL post-governor/post-halt
        # equity slice -> real fills on the DU* paper account. Strictly
        # fail-safe: any error becomes an incident; the internal books are
        # never affected. Runs only when the book actually changed.
        # IBKR calls are bounded (thread + hard timeout) so a hung Gateway
        # socket can never freeze the tick loop (the fund must keep marking /
        # rebalancing / gating even if the broker link is dead).
        ibkr_on = bool(CONFIG.get("ibkr", {}).get("enabled"))
        mirrored = False
        # E40: only mirror while the cash session is OPEN. An overnight sync
        # sizes full-size DAY orders against a target computed on stale
        # closed-market data; that target is materially revised at the first
        # RTH rebalance, and ~85% of the position filled at the open is
        # reversed one sync later (E39: CDNS 279->42, CVX -409->-60, V +176->-7).
        # The orders cannot fill before the open anyway, so placing them early
        # buys nothing and costs a full round-trip. A halt-flatten is exempt:
        # a risk control must always be able to reach the broker.
        session_ok = halt_just_latched or not _session_only() or in_cash_session(now)
        if (rebalanced or halt_just_latched) and ibkr_on and session_ok:
            res = self._ibkr_bounded(
                lambda: self._mirror_to_ibkr(weights["s4"], prices, now), 120)
            if res.get("ok"):
                mirrored = True
            elif res.get("timeout"):
                self._incident("IBKR-SYNC-TIMEOUT",
                               "mirror exceeded 120s - abandoned", now)
            elif res.get("err"):
                self._incident("IBKR-SYNC-FAIL", res["err"], now)
        # Every tick: read-only refresh so the dashboard tracks live IBKR NAV /
        # positions / fills even between rebalances (unless we just mirrored).
        # Fail-quiet: on timeout, record a marker (dashboard shows STALE), no
        # incident spam.
        if ibkr_on and not mirrored:
            res = self._ibkr_bounded(lambda: self._refresh_ibkr(prices, now), 45)
            if res.get("timeout"):
                blk = self.state.setdefault("ibkr", {})
                blk["refresh_error"] = "refresh timed out (Gateway unresponsive)"
                blk["refresh_error_ts"] = now.isoformat()

        # resource metrics (evidence for any future server-upgrade decision)
        try:
            import resource
            import sys as _sys
            raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss_mb = raw / (1024 ** 2) if _sys.platform == "darwin" else raw / 1024
        except Exception:
            rss_mb = 0.0
        self.state["tick_stats"] = {"secs": round(time.time() - t0, 1),
                                    "rss_mb": round(rss_mb, 0),
                                    "rebalanced": rebalanced}
        # Telegram: one daily digest (halt/clear still urgent elsewhere)
        alerts_cfg = CONFIG.get("alerts", {}) or {}
        if alerts_cfg.get("telegram_daily_only", True):
            maybe_send_daily_digest(
                self.state, now,
                hour_utc=int(alerts_cfg.get("digest_hour_utc", 21)))
        self._save()
        return {
            "tick": self.state["ticks"],
            "no_trade": no_trade,
            "rebalanced": rebalanced,
            "equity": {s: round(self.state["systems"][s]["equity"], 2) for s in active},
            "actions": actions,
            "regime": regime,
        }

    def run(self, interval: float = 3600.0, max_ticks: int | None = None) -> None:
        n = 0
        while True:
            try:
                out = self.tick()
                print(f"[{out['tick']}] equity={out['equity']} "
                      f"rebalanced={out['rebalanced']} actions={out['actions']}",
                      flush=True)
            except Exception as exc:  # keep the daemon alive
                print(f"tick error: {exc}", flush=True)
            n += 1
            if max_ticks and n >= max_ticks:
                break
            time.sleep(interval)
