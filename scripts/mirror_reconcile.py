#!/usr/bin/env python3
"""Mirror reconciliation report -- read-only forensics on the fills ledger.

Two live defects escaped every pager, test and dashboard we had, because
nothing measured the mirror against its own source of truth:

  E36 (2026-07-27): data/fills_history.jsonl captured 669 of the 4019
      executions IBKR reported for the session -- 83% of the fills were
      silently lost.  A ledger-count vs broker-count check catches that
      class of defect (the "sink vs source" check below).

  E37 (2026-07-27): the mirror stacked duplicate DAY orders overnight and
      traded $20.25M gross on a $979k NAV to achieve $613k of net
      repositioning -- a ~97% round-trip fraction.  Splitting the same
      ledger into gross vs net notional catches that class, and is the
      verification instrument for the E37 fix: post-fix sessions should show
      round_trip_frac collapsing from ~0.97 toward ~0.

Posture: standalone and read-only, same as scripts/cost_calibration.py.
Stdlib only, no network, no imports from runtime/ or the broker connection
layer, and the ONLY write is the optional --json summary.
"""

import argparse
import collections
import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LEDGER = Path("data/fills_history.jsonl")
DEFAULT_STATE = Path("data/state.json")
JSON_BASENAME = "mirror_reconcile.json"  # written next to the ledger, --json only

# --- flag thresholds -------------------------------------------------------
# Named constants so the bars are auditable and cannot drift silently.
CHURN_FRAC_BAR = 0.30            # >30% of gross notional is pure round-trip
CHURN_GROSS_FLOOR = 1_000_000.0  # ...and only on days big enough to matter
NAV_MULT_BAR = 3.0               # gross > 3x NAV in one session is pathological

BUY_SIDES = ("BOT", "BUY")
SELL_SIDES = ("SLD", "SELL")
REQUIRED_FIELDS = ("id", "symbol", "shares", "price")


def _num(x):
    """Best-effort float, None if the value is missing or not numeric."""
    if x is None or isinstance(x, bool):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _fmt_usd(x):
    return "$%s" % format(round(float(x)), ",d")


def load_ledger(path):
    """Read the append-only fills ledger.  Tolerant by construction.

    Blank lines, corrupt JSON, non-dict rows and rows missing any of
    id/symbol/shares/price are skipped.  Duplicate execIds are dropped,
    first occurrence wins (the ledger is append-only, replays happen).
    """
    path = Path(path)
    if not path.exists():
        return []
    out = []
    seen = set()
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue  # corrupt line: skip, never raise
            if not isinstance(row, dict):
                continue
            bad = False
            for key in REQUIRED_FIELDS:
                val = row.get(key)
                if val is None or (isinstance(val, str) and not val.strip()):
                    bad = True
                    break
            if bad:
                continue
            if _num(row.get("shares")) is None or _num(row.get("price")) is None:
                continue
            exec_id = str(row["id"])
            if exec_id in seen:
                continue  # duplicate execId: first occurrence wins
            seen.add(exec_id)
            out.append(row)
    return out


def load_state(path):
    """Read data/state.json if present.  Never raises; {} on missing/corrupt."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, TypeError, OSError):
        return {}
    return state if isinstance(state, dict) else {}


def day_of(fill):
    """Session date of a fill: 'YYYY-MM-DD' from time, '' when unknown."""
    t = fill.get("time")
    if not isinstance(t, str):
        return ""
    return t[:10]


def nav_of(state):
    """NAV from state['ibkr']['nav'], None when absent or non-positive."""
    if not isinstance(state, dict):
        return None
    ib = state.get("ibkr")
    if not isinstance(ib, dict):
        return None
    nav = _num(ib.get("nav"))
    if nav is None or nav <= 0:
        return None
    return nav


def reconcile_day(fills, nav=None):
    """Gross vs net repositioning for one day's fills (the E37 instrument).

    gross_notional  = sum(shares * price) over every execution
    net_notional    = sum over symbols of |signed notional| (BOT/BUY +,
                      SLD/SELL -), i.e. the repositioning actually achieved
    round_trip_frac = 1 - net/gross, the fraction of turnover that bought
                      and sold the same exposure back (0.97 on 2026-07-27)
    """
    gross = 0.0
    commissions = 0.0
    signed = collections.defaultdict(float)
    sym_gross = collections.defaultdict(float)
    sym_n = collections.Counter()

    for f in fills:
        shares = _num(f.get("shares"))
        price = _num(f.get("price"))
        if shares is None or price is None:
            continue
        notional = abs(shares) * price
        sym = f.get("symbol", "?")
        gross += notional
        sym_gross[sym] += notional
        sym_n[sym] += 1
        side = str(f.get("side", "")).upper()
        if side in SELL_SIDES:
            signed[sym] -= notional
        else:
            signed[sym] += notional  # BOT/BUY (unknown sides counted as buys)
        comm = _num(f.get("commission"))
        if comm is not None:
            commissions += comm

    net = sum(abs(v) for v in signed.values())
    round_trip_frac = (1.0 - net / gross) if gross > 0 else 0.0
    gross_over_nav = (gross / nav) if (nav is not None and nav > 0) else None

    churn = round_trip_frac > CHURN_FRAC_BAR and gross > CHURN_GROSS_FLOOR
    if gross_over_nav is not None and gross_over_nav > NAV_MULT_BAR:
        churn = True
    flags = ["CHURN"] if churn else []

    # Worst offenders first: gross minus net is the wasted turnover.
    rows = []
    for sym, g in sym_gross.items():
        rows.append([sym, g, abs(signed[sym]), sym_n[sym]])
    rows.sort(key=lambda r: (r[1] - r[2]), reverse=True)

    return {
        "n_execs": len(fills),
        "n_symbols": len(sym_gross),
        "gross_notional": gross,
        "net_notional": net,
        "round_trip_frac": round_trip_frac,
        "gross_over_nav": gross_over_nav,
        "commissions": commissions,
        "flags": flags,
        "top_churn": rows[:10],
    }


def check_capture(day_iso, day_n_execs, state):
    """Sink-vs-source fill count check (the E36 instrument).

    The broker's own whole-day execution count lives in
    state['ibkr']['n_fills_today'] and is only comparable when
    state['ibkr']['last_refresh'] falls on the same session date.  Fewer
    rows in the ledger than the broker reported means the ledger dropped
    fills -- exactly the 669-of-4019 defect from 2026-07-27.
    """
    ledger_n = int(day_n_execs or 0)
    out = {"comparable": False, "ledger": ledger_n, "broker": None, "gap": False}
    if not isinstance(state, dict):
        return out
    ib = state.get("ibkr")
    if not isinstance(ib, dict):
        return out
    last = ib.get("last_refresh")
    broker = ib.get("n_fills_today")
    if not isinstance(last, str) or broker is None:
        return out
    if last[:10] != (day_iso or ""):
        return out  # broker counter belongs to another session
    try:
        broker_n = int(broker)
    except (TypeError, ValueError):
        return out
    out["comparable"] = True
    out["broker"] = broker_n
    out["gap"] = ledger_n < broker_n
    return out


def summarize(fills, state=None):
    """Group fills by session date and reconcile each day."""
    nav = nav_of(state)
    by_day = collections.defaultdict(list)
    for f in fills:
        by_day[day_of(f) or "unknown"].append(f)

    days = {iso: reconcile_day(rows, nav=nav) for iso, rows in by_day.items()}
    dated = sorted(iso for iso in days if iso != "unknown")
    order = list(reversed(dated))  # newest first
    if "unknown" in days:
        order.append("unknown")
    newest = dated[-1] if dated else None

    n_newest = days[newest]["n_execs"] if newest is not None else 0
    capture = check_capture(newest, n_newest, state)

    return {
        "n_fills": len(fills),
        "nav": nav,
        "days": days,
        "days_desc": order,
        "newest_day": newest,
        "capture": capture,
    }


def _row_line(iso, day):
    gn = day["gross_over_nav"]
    gn_s = "n/a" if gn is None else ("%.2f" % gn)
    flags = ",".join(day["flags"]) if day["flags"] else "ok"
    return (
        f"{iso:<12}{day['n_execs']:>7}{_fmt_usd(day['gross_notional']):>16}"
        f"{_fmt_usd(day['net_notional']):>16}"
        f"{100.0 * day['round_trip_frac']:>6.1f}%{gn_s:>8}  {flags}"
    )


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Read-only mirror reconciliation: gross vs net repositioning "
                    "and ledger-vs-broker fill capture."
    )
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="fills ledger jsonl")
    ap.add_argument("--state", default=str(DEFAULT_STATE), help="state.json (optional)")
    ap.add_argument("--days", type=int, default=14, help="how many sessions to print")
    ap.add_argument("--json", action="store_true",
                    help="also write mirror_reconcile.json next to the ledger")
    args = ap.parse_args(argv)

    ledger = Path(args.ledger)
    if not ledger.exists():
        print(f"no fills ledger at {ledger} -- nothing to reconcile")
        return 0

    fills = load_ledger(ledger)
    state = load_state(Path(args.state))
    summary = summarize(fills, state)
    nav = summary["nav"]

    print(f"mirror reconciliation  ledger={ledger}  fills={len(fills)}  "
          f"nav={_fmt_usd(nav) if nav else 'unknown'}")
    header = (f"{'date':<12}{'execs':>7}{'gross':>16}{'net':>16}"
              f"{'rt%':>7}{'g/NAV':>8}  flags")
    print(header)
    print("-" * len(header))

    n_days = max(args.days, 0)
    shown = summary["days_desc"][:n_days]
    for iso in shown:
        print(_row_line(iso, summary["days"][iso]))
    if not shown:
        print("(no fills)")

    # Sink vs source for the newest session (E36 defect class).
    cap = summary["capture"]
    newest = summary["newest_day"]
    if newest is None:
        print("capture check: no dated fills in ledger")
    elif cap["comparable"]:
        verdict = "CAPTURE GAP" if cap["gap"] else "ok"
        print(f"capture check {newest}: ledger={cap['ledger']} "
              f"broker={cap['broker']} -> {verdict}")
        if cap["gap"]:
            print(f"  {cap['broker'] - cap['ledger']} broker executions missing "
                  f"from the ledger (E36 defect class)")
    else:
        print(f"capture check {newest}: not comparable "
              f"(no matching broker n_fills_today in state)")

    # Where the churn came from, for anything flagged.
    for iso in shown:
        day = summary["days"][iso]
        if not day["flags"]:
            continue
        print(f"top churn {iso} ({','.join(day['flags'])}):")
        for sym, g, n, k in day["top_churn"]:
            print(f"  {sym:<8}{_fmt_usd(g):>16}{_fmt_usd(n):>16}{k:>7}")

    if args.json:
        payload = dict(summary)
        payload["generated_at"] = datetime.now(timezone.utc).isoformat()
        out_path = ledger.parent / JSON_BASENAME
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True),
                            encoding="utf-8")
        print(f"wrote {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
