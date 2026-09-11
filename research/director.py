"""Research Director (Claude Fable 5) + adversarial Referee (Opus 4.8).

The director THINKS; the harness JUDGES. The director reads the full research
history and designs tomorrow's most informative experiments — inside the
harness grammar, with no ability to pick dates, touch bars, write the ledger,
or reach live config. Its specs are pre-registered at proposal time, so by
the time they run they are immutable.

Fallback chain: Fable-5 (server-side fallback to Opus 4.8 on refusal) ->
plain Opus -> no director (nightly degrades gracefully to the static queue).
Budgets: director 2/night, referee 3/night (registry-tracked).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.env import get_key
from research.harness import (ALLOWED_PARAMS, EXPLORE, HYPOTHESES, LOCKBOX,
                              RESULTS, load_registry, register_spec,
                              save_registry, validate_spec)

MEMOS = Path(__file__).resolve().parent / "NIGHTLY_MEMOS.md"
# E49: the graded ledger is mechanically unwritable forever (correct), so a
# row whose LABEL is wrong has no in-ledger correction channel — corrections
# to what rows MEAN live here and are tailed into the director's context
CORRECTIONS = Path(__file__).resolve().parent / "CORRECTIONS.md"

_DIRECTOR_SYSTEM = f"""You are the research director of a systematic fund.
You design the next experiments; a deterministic harness runs them and judges
them against frozen bars — you cannot grade yourself, choose data windows, or
deploy anything. Exploration data window is {EXPLORE[0]}..today; a lockbox
({LOCKBOX[0]}..{LOCKBOX[1]}) exists that you can never see — candidate
families get ONE confirmation shot there, and a failed shot burns the family
permanently. Every trial you add raises your own multiple-testing bar
(within-family Bonferroni + global DSR deflation), so propose FEWER, more
informative experiments, not sweeps.

`params` is a JSON STRING: emit the parameter object serialised, e.g.
'{{"n_names": 500, "item": null}}'. A spec with no parameters is '{{}}'.

Experiment grammar (the ONLY thing you may emit):
- signal_backtest: w_momentum/w_reversal/w_low_vol (0-2), rebalance_days
  (5|10|21|42), market_neutral (bool), n_names (60-500), period (2y|3y),
  ic_weighting (bool — rolling trailing-IC signal weights, negative-IC
  signals dropped; an adaptive-combining mechanism distinct from static
  blends, so it is not a re-roll of a static blend)
- event_study / event_study_costnet: n_names (60-500),
  item ('2.02'|'5.02'|'1.01'|null), item_not (complement filter — EXCLUDE an
  item class, e.g. item_not='2.02' runs all 8-Ks EXCEPT earnings items in one
  pre-registered trial; null), drift_col ('car2_10'|'car2_20')
- event_portfolio: n_names, item, item_not, entry_lag (1-5), exit_lag (5-21)

Principles: negative results are valuable; do not perturb burned families;
prefer experiments that discriminate between mechanisms; respect that a
family at N trials needs p*N < 0.05.

WHEN YOUR BEST FAMILY IS CLOSED, OPEN A NEW ONE. A family whose status is
`confirmed` or `burned` is closed to further trials FOREVER — the harness
rejects every spec you aim at it, and that rejection is the armor working, not
an obstacle to route around. This has deadlocked you before: four consecutive
proposals into the confirmed `8k-drift` family were rejected and the queue sat
empty for thirteen days, because you had nowhere else you were willing to go.
That is a worse outcome than any experiment you could have run.

The `family` field is FREE TEXT that you choose. Naming a new one is how a
genuinely new mechanism gets its own multiplicity budget instead of inheriting
a saturated family's Bonferroni penalty. So:
  - Do not re-aim at a closed family. Its question is settled; the forward-OOS
    record now answers what is left.
  - Do not dump a new mechanism into a saturated family either. `price-factors`
    is at 11 trials because every static-blend perturbation re-rolls the same
    dice; adding a twelfth makes the bar p<0.0045 for a question you have
    already asked eleven ways.
  - DO open a new family when the mechanism is genuinely different — different
    signal construction, different information source, different holding
    logic. `ic_weighting` (adaptive combining: weights follow realized trailing
    IC, negative-IC signals dropped) is exactly such a mechanism and is
    available in the grammar now; it is not a re-roll of a static blend, and
    it deserves its own family name rather than price-factors' penalty.
  - If you genuinely believe no informative experiment exists tonight, emit
    zero specs and say why. An honest empty night is fine. Four rejected specs
    and an empty queue is not — that is a deadlock, and if you find yourself
    there, say so plainly in the memo so a human can widen the grammar.

If the grammar cannot express an experiment you believe is the most
informative next step, do NOT contort a weaker spec — put the idea in
engineering_wishes (plain language, what primitive/data you need and why).
A staff-engineer lane reads these and may build the primitive the same night
(it applies its own work), so a well-argued wish can grow your grammar by
tomorrow. Wishes that need a human decision — buying data, spending the
lockbox shot, anything touching live capital — say so explicitly."""

_DIRECTOR_SCHEMA = {
    "type": "object",
    "properties": {
        "memo": {"type": "string"},
        "engineering_wishes": {"type": "array", "items": {"type": "string"}},
        "new_specs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "family": {"type": "string"},
                    "type": {"type": "string"},
                    # E45: a JSON STRING, not a typed object. Enumerating all
                    # 13 params inline made the schema 27 nodes, and the API
                    # rejected the whole request with 400 "Schema is too
                    # complex" — silently, from 2026-07-27 (when P0013 added
                    # `ic_weighting`) until 2026-07-30. Nothing is lost by
                    # loosening it: `validate_spec` in the harness is the
                    # authoritative gate and rejects unknown or illegal params
                    # regardless of what the schema allowed through.
                    "params": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["name", "family", "type", "params", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["memo", "new_specs"],
    "additionalProperties": False,
}


def _budget_ok(kind: str, cap: int) -> bool:
    reg = load_registry()
    today = datetime.now(timezone.utc).date().isoformat()
    b = reg["research_budget"]
    if b.get("date") != today:
        b.clear()
        b.update({"date": today})
        save_registry(reg)
    return b.get(kind, 0) < cap


def _budget_spend(kind: str) -> None:
    reg = load_registry()
    reg["research_budget"][kind] = reg["research_budget"].get(kind, 0) + 1
    save_registry(reg)


def research_liveness() -> dict:
    """Is the research engine actually running experiments? (E43)

    The fund's whole purpose is finding edge through the armored harness, and
    it silently produced NOTHING for thirteen days (last result 2026-07-17)
    while four consecutive director specs were rejected into a confirmed
    family and the queue sat empty. Nothing measured it, so nobody saw it —
    the same shape as E36/E37. This makes the stall a number, surfaced to the
    director, the engineer lane and the daily digest.
    """
    from datetime import date

    import yaml

    out: dict = {"days_since_last_experiment": None, "last_experiment": None,
                 "queue_pending": 0, "open_families": [], "closed_families": [],
                 "recent_rejections": [], "recent_errors": []}
    try:
        rows = [json.loads(l) for l in RESULTS.read_text().splitlines() if l.strip()]
    except (OSError, json.JSONDecodeError):
        rows = []
    if rows:
        last = rows[-1]
        out["last_experiment"] = {"id": last.get("id"),
                                  "name": (last.get("spec") or {}).get("name"),
                                  "verdict": last.get("verdict"),
                                  "ts": str(last.get("ts", ""))[:10]}
        try:
            d = date.fromisoformat(str(last.get("ts", ""))[:10])
            out["days_since_last_experiment"] = (date.today() - d).days
        except ValueError:
            pass
        # E59 — an ERROR verdict must carry its cause: without it a failed run
        # renders identically to an empty one, and N0026 sat verdict=ERROR for
        # three nights while its exception string was on disk (the E45 rule)
        if last.get("error"):
            out["last_experiment"]["error"] = str(last["error"])[:300]
        # bounded on purpose — this dict is embedded in two model contexts and
        # the nightly JSON digest: scan 15, report 3, truncate at 300 chars
        out["recent_errors"] = [
            {"id": r.get("id"), "name": (r.get("spec") or {}).get("name"),
             "error": str(r.get("error"))[:300]}
            for r in rows[-15:]
            if r.get("verdict") == "ERROR" and r.get("error")][-3:]

    reg = load_registry()
    for name, fam in (reg.get("families") or {}).items():
        (out["closed_families"] if fam.get("status") in ("burned", "confirmed")
         else out["open_families"]).append(f"{name}({fam.get('status')},"
                                           f"{fam.get('trials')} trials)")
    try:
        queue = yaml.safe_load(HYPOTHESES.read_text()) or []
    except (OSError, yaml.YAMLError):
        # deliberately NOT a bare `except Exception`: that swallowed a
        # NameError here during development and silently reported an empty
        # queue — a liveness probe that lies is worse than none.
        queue = []
    out["queue_pending"] = sum(1 for h in queue
                               if h.get("status", "pending") == "pending")
    out["recent_rejections"] = [
        {"name": h.get("name"), "why": h.get("reject_reason")}
        for h in queue if h.get("status") == "rejected"][-4:]

    stalled = (out["days_since_last_experiment"] or 0) >= 3 and not out["queue_pending"]
    out["STALLED"] = stalled
    if stalled:
        out["note"] = ("No experiment has run in "
                       f"{out['days_since_last_experiment']} days and the queue "
                       "is empty. If your specs keep being rejected for the same "
                       "reason, that reason is the finding — say so.")
    return out


# maps a confirmed family to the live book that trades it forward (S5 enabled
# 2026-07-26 as the forward shadow of the confirmed calendar-time spec); new
# confirmed families get an entry here when their sleeve goes live, and a
# confirmed family with no entry is reported with book=None and a note rather
# than being dropped
FORWARD_BOOKS = {"8k-drift": "s5"}


def forward_oos(state_path=None, equity_ledger_path=None) -> dict:
    """Is the one CONFIRMED family still accruing, forward and out of sample?

    `8k-drift` is confirmed and therefore closed to trials FOREVER, so S5's
    forward paper record is the only evidence stream left on the fund's single
    confirmed edge — and nothing compared that record to the lockbox
    expectation it was confirmed on, so neither the director (whose context
    carries no book performance at all) nor the engineer lane (which sees raw
    equity) could say whether the edge is accruing or decaying.

    Diagnostic ONLY: no decision rule reads this, and no read is meaningful
    before ~60 forward days. Strictly read-only, and a missing data file is a
    DATA condition rather than an error — the proposal sandbox strips data/
    entirely, so a well-formed dict has to come back there too.
    """
    import pandas as pd

    from backtest.metrics import max_drawdown, sharpe_ratio

    repo_root = Path(__file__).resolve().parent.parent
    state_path = Path(state_path or repo_root / "data" / "state.json")
    equity_ledger_path = Path(equity_ledger_path
                              or repo_root / "data" / "equity_daily.jsonl")

    def _ledger_closes(path: Path, book: str) -> list:
        """Daily closes for one book out of the JSONL equity ledger (P0017)."""
        by_date: dict = {}
        try:
            lines = path.read_text().splitlines()
        except (OSError, json.JSONDecodeError, ValueError, TypeError, KeyError):
            return []
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or row.get("series") != book:
                    continue
                day = str(row.get("date") or "")[:10]
                if day:
                    by_date[day] = float(row.get("value"))     # last row wins
            except (OSError, json.JSONDecodeError, ValueError, TypeError, KeyError):
                continue     # one malformed line must not blank the whole read
        return sorted(by_date.items())

    def _state_closes(path: Path, book: str) -> list:
        """Fallback: collapse state.json's intraday marks to daily closes."""
        by_date: dict = {}
        try:
            s = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, ValueError, TypeError, KeyError):
            return []
        eh = s.get("equity_history") if isinstance(s, dict) else None
        hist = eh.get(book) if isinstance(eh, dict) else None
        if not isinstance(hist, list):
            return []
        for e in hist:
            if not isinstance(e, (list, tuple)) or len(e) < 2:
                continue
            day = str(e[0])[:10]
            if not day:
                continue
            try:
                by_date[day] = float(e[1])        # last mark of the date wins
            except (ValueError, TypeError):
                continue
        return sorted(by_date.items())

    fams = [(n, f) for n, f in (load_registry().get("families") or {}).items()
            if isinstance(f, dict) and f.get("status") == "confirmed"]
    if not fams:
        return {"diagnostic_only": True, "families": [],
                "note": "no confirmed families"}

    # the expectation each family was graded against, read off its own
    # confirmation row rather than hardcoded here
    lockbox: dict = {}
    try:
        for line in RESULTS.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (isinstance(row, dict) and row.get("phase") == "confirmation"
                    and row.get("verdict") == "CONFIRMED"):
                m = row.get("metrics")
                if isinstance(m, dict):
                    lockbox[row.get("family")] = {
                        k: m.get(k) for k in ("sharpe", "ann_return",
                                              "max_dd", "n_days")}
    except (OSError, json.JSONDecodeError, ValueError, TypeError, KeyError):
        lockbox = {}

    out: list = []
    for name, fam in fams:
        book = FORWARD_BOOKS.get(name)
        entry: dict = {"family": name, "book": book, "n_days": 0}
        if book is None:
            entry["note"] = ("no forward book mapped — add one to FORWARD_BOOKS "
                             "when this family's sleeve goes live")
        else:
            closes = _ledger_closes(equity_ledger_path, book)
            source = "equity_daily.jsonl"
            if len(closes) < 2:
                closes = _state_closes(state_path, book)
                source = "state.json:equity_history"
            values = [v for _, v in closes]
            rets = [values[i] / values[i - 1] - 1.0
                    for i in range(1, len(values)) if values[i - 1]]
            if not rets:
                entry["note"] = "no equity data yet"
            else:
                r = pd.Series(rets, dtype="float64")
                entry.update({
                    "n_days": len(rets),
                    "first_date": closes[0][0], "last_date": closes[-1][0],
                    "source": source,
                    "fwd": {
                        # under 8 forward days a Sharpe is noise dressed as
                        # precision, so it is not printed at all
                        "sharpe": (round(float(sharpe_ratio(r)), 3)
                                   if len(rets) >= 8 else None),
                        "ann_return": round(float(r.mean()) * 252, 4),
                        "ann_vol": (round(float(r.std()) * 252 ** 0.5, 4)
                                    if len(rets) >= 2 else None),
                        "max_dd": round(float(max_drawdown(
                            pd.Series(values, dtype="float64"))), 4),
                        "total_ret_pct": (round((values[-1] / values[0] - 1) * 100, 3)
                                          if values[0] else None),
                    }})
        entry["lockbox"] = lockbox.get(name)
        out.append(entry)

    return {"diagnostic_only": True,
            "note": ("accrual instrument — the family is closed to trials; NO "
                     "decision rule reads this, and no read is meaningful "
                     "before ~60 forward days"),
            "families": out}


def _context() -> str:
    parts = []
    log = Path(__file__).resolve().parent.parent / "RESEARCH_LOG.md"
    if log.exists():
        parts.append("=== RESEARCH_LOG (tail) ===\n" + "\n".join(
            log.read_text().splitlines()[-260:]))
    if CORRECTIONS.exists():
        # E49: absent file = no corrections on record, a normal state; a read
        # that BLOWS UP is named, not swallowed (E45 rule) — a failure must be
        # distinguishable from a decision not to act
        try:
            parts.append("=== RECORD CORRECTIONS (append-only; the results "
                         "ledger is mechanically unwritable, so corrections to "
                         "what its rows MEAN live here) ===\n" + "\n".join(
                             CORRECTIONS.read_text().splitlines()[-150:]))
        except Exception as exc:
            parts.append(f"=== RECORD CORRECTIONS === unavailable: "
                         f"{type(exc).__name__}: {exc}")
    if RESULTS.exists():
        rows = [json.loads(l) for l in RESULTS.read_text().splitlines()[-25:]]
        # E59 — an ERROR row must carry its cause; N0026's exception string sat
        # on disk in this very file for three nights while both context builders
        # slimmed it out (the E45 rule, one layer down)
        slim = []
        for r in rows:
            row = {"id": r["id"], "name": r["spec"].get("name"),
                   "family": r.get("family"), "phase": r.get("phase"),
                   "verdict": r["verdict"], "metrics": r["metrics"]}
            if r.get("error"):
                row["error"] = str(r["error"])[:300]
            slim.append(row)
        parts.append("=== RECENT RESULTS ===\n" + json.dumps(slim, indent=1))
    reg = load_registry()
    parts.append("=== FAMILIES ===\n" + json.dumps(reg["families"], indent=1))
    parts.append(f"=== GLOBAL N === {reg['total_experiments']}")
    parts.append("=== RESEARCH LIVENESS ===\n" + json.dumps(research_liveness(),
                                                            indent=1, default=str))
    try:
        parts.append("=== FORWARD OOS (confirmed families — diagnostic only) ===\n"
                     + json.dumps(forward_oos(), indent=1, default=str))
    except Exception as exc:
        # E45: a failure must be distinguishable from a decision not to act —
        # a silently missing section reads as "nothing to report"
        parts.append(f"=== FORWARD OOS === unavailable: "
                     f"{type(exc).__name__}: {exc}")
    return "\n\n".join(parts)


def run_director(queue: list[dict]) -> dict | None:
    """One Fable design session -> memo + 0..3 validated, PRE-REGISTERED specs."""
    key = get_key("ANTHROPIC_API_KEY")
    if not key:
        return None                       # no key: a decision not to act
    # E57b: budget exhaustion used to share that `return None`, so a capped
    # director printed nothing at all - the nightly digest simply had no
    # director line and the run looked like the director had chosen silence.
    # Same residue E45 left in the error path, and the same rule applies: a
    # limit doing its job should say so. The engineer already names its cap.
    if not _budget_ok("director", 2):
        return {"skipped": "daily design budget spent (2/day)"}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key, timeout=600)
        pending = [h for h in queue if h.get("status", "pending") == "pending"]
        user_msg = (_context()
                    + "\n\n=== CURRENT QUEUE (pending) ===\n"
                    + json.dumps([{k: h.get(k) for k in ("name", "family", "type",
                                                          "params")} for h in pending],
                                 indent=1)
                    + "\n\nWrite your research memo (what we now know, what the "
                      "sharpest open question is) and propose 0-3 next experiments.")
        with client.beta.messages.stream(
            model="claude-fable-5",
            max_tokens=16000,
            betas=["server-side-fallback-2026-06-01"],
            fallbacks=[{"model": "claude-opus-4-8"}],
            system=_DIRECTOR_SYSTEM,
            output_config={"format": {"type": "json_schema",
                                      "schema": _DIRECTOR_SCHEMA}},
            messages=[{"role": "user", "content": user_msg}],
        ) as stream:
            resp = stream.get_final_message()
        _budget_spend("director")
        if resp.stop_reason == "refusal":
            return None
        out = json.loads(next(b.text for b in resp.content if b.type == "text"))

        accepted, rejected = [], []
        for spec in out.get("new_specs", [])[:3]:
            raw = spec.get("params")
            if isinstance(raw, str):          # E45 wire format
                try:
                    raw = json.loads(raw or "{}")
                except json.JSONDecodeError as exc:
                    rejected.append({"name": spec.get("name"),
                                     "error": f"params not JSON: {exc}"})
                    continue
            if not isinstance(raw, dict):
                rejected.append({"name": spec.get("name"),
                                 "error": f"params must be an object, got {type(raw).__name__}"})
                continue
            spec["params"] = {k: v for k, v in raw.items() if v is not None}
            err = validate_spec(spec)
            if err:
                rejected.append({"name": spec.get("name"), "error": err})
                continue
            spec["source"] = "director"
            spec["status"] = "pending"
            spec["prereg"] = register_spec(spec)      # immutable from here on
            accepted.append(spec)

        memo = out.get("memo", "")
        wishes = [w for w in out.get("engineering_wishes", []) if w.strip()]
        if wishes:
            # routed to the staff-engineer lane (E22): wishes become context
            # for code proposals that can grow the experiment grammar
            wf = Path(__file__).resolve().parent / "WISHES.md"
            with open(wf, "a") as fh:
                fh.write(f"\n## {datetime.now(timezone.utc).date().isoformat()}\n"
                         + "".join(f"- {w}\n" for w in wishes))
        with open(MEMOS, "a") as fh:
            fh.write(f"\n## {datetime.now(timezone.utc).date().isoformat()} "
                     f"(model={resp.model})\n{memo}\n"
                     + "".join(f"- queued: {s['name']} [{s['family']}]\n"
                               for s in accepted)
                     + "".join(f"- rejected: {r['name']} ({r['error']})\n"
                               for r in rejected))
        return {"memo": memo, "accepted": accepted, "rejected": rejected,
                "model": resp.model}
    except Exception as exc:
        # E45: this was `return None`, and a 400 "Schema is too complex" hid
        # behind it for three days — the research engine was completely dead
        # and the nightly digest said nothing, because None is also what a
        # missing key or an exhausted budget returns. A failure must be
        # distinguishable from a decision not to act.
        return {"error": f"{type(exc).__name__}: {exc}"[:400]}


def run_referee(entry: dict) -> str | None:
    """Adversarial Opus memo on a PASS/CONFIRMED result. Cannot block —
    the judge is code — but its objections travel with the result."""
    key = get_key("ANTHROPIC_API_KEY")
    if not key or not _budget_ok("referee", 3):
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key, timeout=180)
        resp = client.messages.create(
            # E34: opus-5 — same price as 4.8 ($5/$25), stronger judgment; the
            # referee is not a live book, so no pre-registration clock applies
            model="claude-opus-5",
            max_tokens=2000,
            thinking={"type": "adaptive"},
            system=("You are an adversarial quant referee. Given an experiment "
                    "result, list the strongest concrete reasons it could be an "
                    "artifact (overlap inflation, survivorship, costs, regime "
                    "concentration, family mining, lookahead). Be specific and "
                    "brief (<=200 words). You cannot block it; your memo goes "
                    "to the human reviewer."),
            messages=[{"role": "user", "content": json.dumps(
                {k: entry.get(k) for k in ("spec", "metrics", "verdict",
                                           "family", "family_trials", "phase")})}],
        )
        _budget_spend("referee")
        if resp.stop_reason == "refusal":
            return None
        return next((b.text for b in resp.content if b.type == "text"), None)
    except Exception:
        return None
