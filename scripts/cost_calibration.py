"""Cost-surface calibration report (director wish 2026-07-21).

Joins the IBKR paper mirror's realized fills against the CostModel prediction
for the same trades, using the per-name 20d ADV / 30d daily-vol inputs the
live loop stamps at each rebalance (state.cost_inputs). This is the validation
step the E23 single-cost-surface work was waiting for: "costs stay pessimistic"
is only proven when modeled costs BOUND realized costs on real executions.

Fill inputs: the append-only data/fills_history.jsonl ledger (every fill the
runtime has ever merged — see runtime/live.py) plus the state.ibkr.fills ring
(300 rows, dashboard). analyze_fills dedupes by execId, so overlap between
the two sources is harmless.

Calibration set: clean-execution fills only. Every mirror session through
2026-07-29 ran under known defects — E37 order stacking (cancel-then-plan
shipped intra-day 07-27) and the E39/E40 full-size overnight order sized off a
stale closed-market target that the first RTH rebalance reversed (session gate
shipped late 07-29). scripts/mirror_reconcile.py measured 97.0% round-trip at
21.1x NAV on 07-27, 84.3% at 3.28x on 07-28, 91.0% at 7.34x on 07-29: fitting
impact_coef to that self-inflicted churn would fit the model to a bug. So fills
before CLEAN_START are excluded by default, as is any LATER session the
reconciliation instrument flags CHURN (that means the mirror has regressed).
--include-churn restores them for forensics only.

Reference price for realized slippage: the PRIOR trading day's close — the
decision price the mirror's marketable limits were sized from, and exactly
the mid the CostModel models slippage against — fetched via Polygon
grouped-daily (one call per unique date, cached). With no key or no network,
slippage is reported as n/a and commissions are still calibrated.

Deliberately read-only and standalone: never writes state.json, touches no
runtime path. Honest flag it will raise immediately: config models equity
commission at 0 bps (zero-commission-broker framing) while IBKR charges real
commissions — if realized exceeds modeled, the pessimism invariant says a
HUMAN should raise the modeled parameter; this report only surfaces the
evidence, it never edits config.

Run:  python scripts/cost_calibration.py            # report to stdout
      python scripts/cost_calibration.py --json     # + data/cost_calibration.json
      python scripts/cost_calibration.py --include-churn   # forensics only
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.broker.costs import (FALLBACK_ADV, FALLBACK_DVOL, CostModel,
                               CostParams)
from core.config import CONFIG

# Pessimistic fallbacks — MUST match runtime/live.py, so modeled-vs-realized
# compares against exactly the surface the books were actually charged.
# Imported, not restated: this script compares modeled against realized, so
# it must charge exactly the surface the books were charged.

FILLS_LEDGER = Path("data/fills_history.jsonl")

# First session executed under BOTH mirror fixes (E37 cancel-then-plan, E40
# session gate) with scripts/mirror_reconcile.py showing clean numbers: 185
# execs, 11% round-trip, 0.57x NAV. Everything 2026-07-24..07-29 ran under
# order stacking and/or the stale-plan overnight reversal, i.e. self-inflicted
# churn rather than market impact, so those fills are not calibration data.
# Moving this date is a HUMAN decision — it defines the clean dataset the
# impact_coef read is computed on.
CLEAN_START = "2026-07-30"

NOTIONAL_BUCKETS = [
    (0.0, 2_000.0, "<$2k"),
    (2_000.0, 10_000.0, "$2k-$10k"),
    (10_000.0, 50_000.0, "$10k-$50k"),
    (50_000.0, float("inf"), ">$50k"),
]


def bucket_of(notional: float) -> str:
    for lo, hi, label in NOTIONAL_BUCKETS:
        if lo <= notional < hi:
            return label
    return NOTIONAL_BUCKETS[-1][2]


def _round(v, nd: int = 3):
    return None if v is None else round(v, nd)


def load_fills(state: dict, ledger_path: Path = FILLS_LEDGER) -> list:
    """Every fill we know about: the append-only ledger plus the state ring.

    The ring is capped at 300 rows (dashboard); the ledger keeps everything
    (runtime/live.py appends every new fill). Overlap and the rare ring-
    eviction re-admit are expected — analyze_fills dedupes by execId, first
    occurrence wins, so ledger rows lead.
    """
    fills: list = []
    if ledger_path.exists():
        for line in ledger_path.read_text().splitlines():
            try:
                fills.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    fills.extend((state.get("ibkr") or {}).get("fills", []))
    return fills


# ------------------------------------------------- clean-execution filter --
def _churn_days(fills: list, state: dict) -> set[str] | None:
    """ISO dates the reconciliation instrument flags CHURN, or None.

    Loads scripts/mirror_reconcile.py BY PATH (the lazy pattern
    research/engineer.py's _mirror_view uses) so this script keeps working when
    run as `python scripts/cost_calibration.py`. Any failure at all —
    instrument absent, summary shape changed, input it dislikes — returns None,
    meaning "churn layer unavailable"; the caller then applies the date cutoff
    alone. Never raises.
    """
    try:
        import importlib.util

        path = Path(__file__).resolve().parent / "mirror_reconcile.py"
        spec = importlib.util.spec_from_file_location("mirror_reconcile", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        summary = mod.summarize(fills, state)

        days = summary
        if isinstance(summary, dict):
            days = None
            for key in ("days", "by_day", "sessions", "daily"):
                if isinstance(summary.get(key), (list, dict)):
                    days = summary[key]
                    break
            if days is None:                # unfamiliar shape: find day rows
                for v in summary.values():
                    if (isinstance(v, list) and v and isinstance(v[0], dict)
                            and "flags" in v[0]):
                        days = v
                        break
        entries = (list(days.items()) if isinstance(days, dict)
                   else [(None, d) for d in (days or [])])

        flagged: set[str] = set()
        for key, entry in entries:
            if not isinstance(entry, dict):
                continue
            if not any("CHURN" in str(f).upper()
                       for f in (entry.get("flags") or [])):
                continue
            day = entry.get("date") or entry.get("day") or key
            if day:
                flagged.add(str(day)[:10])
        return flagged
    except Exception:
        return None


def filter_calibration_fills(fills: list, state: dict,
                             include_churn: bool = False) -> tuple[list, dict]:
    """The calibration set — clean-execution fills only — plus a report.

    Total: malformed rows are skipped and counted, never raised. The only I/O
    is the by-path instrument load inside _churn_days. Exclusions:
      * every fill before CLEAN_START (the mirror-defect era: E37 stacking,
        E39/E40 stale-plan reversal — self-inflicted churn, not impact);
      * every fill on a post-CLEAN_START session the reconciliation instrument
        flags CHURN, i.e. a mirror REGRESSION, so a defect can never silently
        poison impact_coef again;
      * every fill whose timestamp will not parse — an undatable fill cannot
        be certified clean.
    include_churn=True is the forensics override: dedupe only, nothing filtered.
    """
    report = {
        "n_input": len(fills or []),
        "n_kept": 0,
        "n_pre_clean": 0,
        "pre_clean_days": [],
        "n_churn_flagged": 0,
        "churn_days": [],
        "n_undated": 0,
        "n_no_id": 0,
        "clean_start": CLEAN_START,
        "include_churn": bool(include_churn),
        "churn_layer": "ok",
    }

    # dedupe by execId first — same rule analyze_fills uses, ledger rows lead
    deduped: list = []
    seen: set[str] = set()
    for f in (fills or []):
        try:
            fid = str(f.get("id") or "")
        except Exception:
            report["n_no_id"] += 1          # not even a fill-shaped row
            continue
        if not fid:
            report["n_no_id"] += 1
            continue
        if fid in seen:
            continue
        seen.add(fid)
        deduped.append(f)

    if include_churn:
        report["n_kept"] = len(deduped)
        report["churn_layer"] = "bypassed"
        return deduped, report

    dated: list = []
    pre_clean: set[str] = set()
    for f in deduped:
        day = str(f.get("time"))[:10]
        try:
            date.fromisoformat(day)
        except (TypeError, ValueError):
            report["n_undated"] += 1
            continue
        if day < CLEAN_START:
            report["n_pre_clean"] += 1
            pre_clean.add(day)
            continue
        dated.append(f)
    report["pre_clean_days"] = sorted(pre_clean)

    churn = _churn_days(dated, state or {}) if dated else set()
    if churn is None:
        report["churn_layer"] = "unavailable"
        churn = set()

    kept: list = []
    hit: set[str] = set()
    for f in dated:
        day = str(f.get("time"))[:10]
        if day in churn:
            report["n_churn_flagged"] += 1
            hit.add(day)
            continue
        kept.append(f)
    report["churn_days"] = sorted(hit)
    report["n_kept"] = len(kept)
    return kept, report


# ---------------------------------------------------------------- analysis --
def analyze_fills(fills: list, cost_inputs: dict, ref_price_fn) -> list[dict]:
    """Per-fill realized-vs-modeled cost rows.

    Pure given ref_price_fn(symbol, fill_date_iso) -> prior close | None;
    any exception from the fetcher degrades to slippage=None, never raises.
    The mirror is equities-only, so the equities cost params apply throughout.
    """
    cm = CostModel(CostParams(**CONFIG.costs.equities))
    rows: list[dict] = []
    seen: set[str] = set()
    for f in fills:
        fid = str(f.get("id") or "")
        if not fid or fid in seen:
            continue
        seen.add(fid)
        try:
            shares = abs(int(f.get("shares") or 0))
            px = float(f.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        if shares <= 0 or px <= 0:
            continue
        sym = str(f.get("symbol") or "?")
        notional = shares * px
        inp = cost_inputs.get(sym) or []
        adv = float(inp[0]) if len(inp) > 0 and inp[0] and inp[0] > 0 else FALLBACK_ADV
        dvol = float(inp[1]) if len(inp) > 1 and inp[1] and inp[1] > 0 else FALLBACK_DVOL
        bd = cm.estimate(notional, adv=adv, daily_vol=dvol)

        comm = f.get("commission")
        realized_comm_bps = (abs(float(comm)) / notional * 1e4
                             if comm is not None else None)

        side = str(f.get("side") or "").upper()
        buy = side in ("BOT", "BUY")
        day = str(f.get("time") or "")[:10]
        try:
            ref = ref_price_fn(sym, day)
        except Exception:
            ref = None
        slip_bps = None
        if ref is not None and ref > 0:
            slip = (px - ref) / ref if buy else (ref - px) / ref
            slip_bps = slip * 1e4          # positive = cost paid vs decision px

        row = {
            "symbol": sym, "date": day, "side": side,
            "shares": shares, "price": px, "notional": round(notional, 2),
            "participation": round(notional / adv, 8),
            "bucket": bucket_of(notional),
            "modeled_slippage_bps": round(bd.slippage * 1e4, 3),
            "modeled_commission_bps": round(bd.commission * 1e4, 3),
            "modeled_total_bps": round(bd.total * 1e4, 3),
            "realized_commission_bps": _round(realized_comm_bps),
            "realized_slippage_bps": _round(slip_bps),
        }
        row["realized_total_bps"] = (
            round(row["realized_slippage_bps"] + row["realized_commission_bps"], 3)
            if row["realized_slippage_bps"] is not None
            and row["realized_commission_bps"] is not None else None)
        rows.append(row)
    return rows


def _wavg(rows: list[dict], key: str):
    """Notional-weighted average of `key` over rows where it is present."""
    vals = [(r[key], r["notional"]) for r in rows if r.get(key) is not None]
    tot = sum(n for _, n in vals)
    if tot <= 0:
        return None
    return sum(v * n for v, n in vals) / tot


def summarize(rows: list[dict]) -> dict:
    """Aggregate calibration read: does the modeled surface bound reality?"""
    priced = [r for r in rows if r["realized_total_bps"] is not None]
    buckets: dict[str, dict] = {}
    for _, _, label in NOTIONAL_BUCKETS:
        rs = [r for r in rows if r["bucket"] == label]
        if not rs:
            continue
        buckets[label] = {
            "n": len(rs),
            "notional": round(sum(r["notional"] for r in rs), 2),
            "modeled_bps": _round(_wavg(rs, "modeled_total_bps"), 2),
            "realized_slip_bps": _round(_wavg(rs, "realized_slippage_bps"), 2),
            "realized_comm_bps": _round(_wavg(rs, "realized_commission_bps"), 2),
        }
    gaps = sorted(priced,
                  key=lambda r: r["realized_total_bps"] - r["modeled_total_bps"],
                  reverse=True)
    return {
        "n_fills": len(rows),
        "n_with_slippage": len(priced),
        "total_notional": round(sum(r["notional"] for r in rows), 2),
        "modeled_bps_wavg": _round(_wavg(rows, "modeled_total_bps"), 2),
        "realized_bps_wavg": _round(_wavg(priced, "realized_total_bps"), 2),
        "realized_comm_bps_wavg": _round(_wavg(rows, "realized_commission_bps"), 2),
        "frac_modeled_bounds_realized": (
            round(sum(1 for r in priced
                      if r["modeled_total_bps"] >= r["realized_total_bps"])
                  / len(priced), 3) if priced else None),
        "buckets": buckets,
        "worst_fills": [{k: r[k] for k in ("symbol", "date", "side", "notional",
                                            "modeled_total_bps",
                                            "realized_total_bps")}
                        for r in gaps[:5]],
    }


# ---------------------------------------------------------- reference price --
def make_prior_close_fn(max_back: int = 5):
    """Prior trading day's close via Polygon grouped-daily, cached per date.

    Returns fn(symbol, fill_date_iso) -> float | None. No key -> always None
    (commissions still calibrate; slippage shows as n/a). Never raises.
    """
    from core.env import has_key
    if not has_key("POLYGON_API_KEY"):
        return lambda sym, day: None
    from core.data.polygon import PolygonDataProvider
    prov = PolygonDataProvider()
    cache: dict[str, dict] = {}

    def grouped(day_iso: str) -> dict:
        if day_iso not in cache:
            try:
                cache[day_iso] = prov.grouped_daily(day_iso)
            except Exception:
                cache[day_iso] = {}
        return cache[day_iso]

    def fn(sym: str, day: str):
        if not day:
            return None
        try:
            d = date.fromisoformat(day)
        except ValueError:
            return None
        for _ in range(max_back):
            d -= timedelta(days=1)
            bars = grouped(d.isoformat())
            if len(bars) > 100:            # a real trading day, not a holiday
                b = bars.get(sym)
                return float(b["close"]) if b and b.get("close") else None
        return None

    return fn


# ------------------------------------------------------------------- report --
def format_report(summary: dict) -> str:
    lines = ["=== COST-SURFACE CALIBRATION — realized IBKR fills vs CostModel ==="]
    lines.append(
        f"fills: {summary['n_fills']} ({summary['n_with_slippage']} with "
        f"reference price) · notional ${summary['total_notional']:,.0f}")
    lines.append(
        f"modeled total (w-avg): {summary['modeled_bps_wavg']} bps · "
        f"realized total (w-avg): {summary['realized_bps_wavg']} bps · "
        f"realized commission (w-avg): {summary['realized_comm_bps_wavg']} bps")
    frac = summary["frac_modeled_bounds_realized"]
    lines.append(
        "modeled bounds realized: "
        + ("n/a (no priced fills)" if frac is None else f"{frac:.0%} of priced fills")
        + "  — the pessimism invariant wants this at 100%")
    lines.append("by notional bucket:")
    for label, b in summary["buckets"].items():
        lines.append(
            f"  {label:>10}  n={b['n']:<4} ${b['notional']:>12,.0f}  "
            f"modeled {b['modeled_bps']} bps  "
            f"realized slip {b['realized_slip_bps']} bps  "
            f"comm {b['realized_comm_bps']} bps")
    if summary["worst_fills"]:
        lines.append("worst fills (realized − modeled):")
        for w in summary["worst_fills"]:
            lines.append(
                f"  {w['symbol']:<8} {w['date']} {w['side']:<4} "
                f"${w['notional']:>10,.0f}  modeled {w['modeled_total_bps']} bps  "
                f"realized {w['realized_total_bps']} bps")
    comm_cfg = CONFIG.costs.equities.get("commission_bps", 0.0)
    lines.append(
        f"NOTE: equity commission is modeled at {comm_cfg} bps in config; IBKR "
        "charges real commissions. If realized > modeled persists, raising the "
        "modeled parameter is a HUMAN decision — this report never edits config.")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="data/state.json")
    ap.add_argument("--json", action="store_true",
                    help="also write data/cost_calibration.json")
    ap.add_argument("--include-churn", action="store_true",
                    help="restore defect-era (pre-CLEAN_START) and CHURN-flagged "
                         "fills — FORENSICS ONLY; calibrating on them fits the "
                         "cost model to a mirror bug")
    args = ap.parse_args()

    state_path = Path(args.state)
    if not state_path.exists():
        print(f"no state file at {state_path}")
        return
    state = json.loads(state_path.read_text())
    fills = load_fills(state)
    if not fills:
        print("no IBKR fills recorded yet (ring or ledger) — nothing to calibrate")
        return
    kept, freport = filter_calibration_fills(
        fills, state, include_churn=args.include_churn)
    print(f"calibration set: kept {freport['n_kept']} of {freport['n_input']} "
          f"fills (excluded: {freport['n_pre_clean']} pre-{CLEAN_START} across "
          f"{len(freport['pre_clean_days'])} sessions, "
          f"{freport['n_churn_flagged']} churn-flagged {freport['churn_days']}, "
          f"{freport['n_undated']} undated)")
    if freport["n_churn_flagged"] > 0:
        print(f"WARNING: post-CLEAN_START CHURN session(s) {freport['churn_days']}"
              " — mirror regression, investigate with scripts/mirror_reconcile.py")
    if not kept:
        print("no clean fills yet — the clean-execution dataset starts "
              f"{CLEAN_START}")
        return
    rows = analyze_fills(kept, state.get("cost_inputs", {}),
                         make_prior_close_fn())
    summary = summarize(rows)
    print(format_report(summary))
    if args.json:
        out = Path("data") / "cost_calibration.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "generated": datetime.now(timezone.utc).isoformat(),
            "summary": summary, "rows": rows}, indent=1))
        print(f"written {out}")


if __name__ == "__main__":
    main()
