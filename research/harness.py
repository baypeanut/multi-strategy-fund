"""Armored research harness v2 — the deterministic judge (E17).

Anti-snooping architecture (all mechanical, none rely on trusting the agent):
  REGISTRATION  every spec is hash+timestamp registered BEFORE it can run;
                any edit is a new spec and a new trial (N increments).
  FAMILIES      every spec belongs to a hypothesis family; verdicts must clear
                the global bar AND a within-family Bonferroni (p * trials).
  LOCKBOX       exploration only ever sees EXPLORE window data; a family whose
                discovery passes gets exactly ONE confirmation shot on the
                untouched LOCKBOX window (calendar-time portfolio, cost-net).
                Fail -> family BURNED: no more trials accepted, ever.
  FROZEN BARS   thresholds live here in code, hashed into every result row.
  STAMPING      every result carries spec/harness/universe hashes and windows.
  Agent NEVER writes this ledger, NEVER computes metrics, NEVER touches live.

Windows (fixed by design — the agent cannot choose dates):
  EXPLORE  2023-07-15 .. today   (all past experiments lived here)
  LOCKBOX  2021-07-15 .. 2023-07-14  (never touched by any run to date;
           chosen over "most recent" because F1b's pooled window already
           consumed recent data — see E16/E17)
"""
from __future__ import annotations

import hashlib
import json
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scipy.stats import norm

ROOT = Path(__file__).resolve().parent
REGISTRY = ROOT / "registry.json"
RESULTS = ROOT / "RESULTS.jsonl"
HYPOTHESES = ROOT / "hypotheses.yaml"

# --- frozen pre-registered bars (changing these is itself a logged event) ---
DSR_BAR = 0.95            # signal backtests, discovery
EVENT_T_BAR = 2.5         # event studies, discovery (pre-family-penalty)
FAMILY_ALPHA = 0.05       # within-family Bonferroni budget
CONFIRM_T_BAR = 2.0       # lockbox calendar-time portfolio, one-shot
EXPLORE = ("2023-07-15", date.today().isoformat())
LOCKBOX = ("2021-07-15", "2023-07-14")

# item_not: complement filter (director wish 2026-07-19) — e.g. item_not='2.02'
# expresses 'all 8-Ks EXCEPT earnings items' as ONE pre-registered trial
# instead of differencing two runs. Grammar addition only; the bars, windows,
# verdict logic, and lockbox mechanics above are untouched.
ALLOWED_PARAMS = {
    # ic_weighting: rolling trailing-IC signal weights (director wish
    # 2026-07-26) — adaptive combining, mechanistically distinct from a static
    # blend re-roll. Grammar addition only; the bars, windows, verdict logic,
    # and lockbox mechanics above are untouched.
    "signal_backtest": {"w_momentum", "w_reversal", "w_low_vol", "rebalance_days",
                        "market_neutral", "n_names", "period", "ic_weighting"},
    "event_study": {"n_names", "item", "item_not", "drift_col"},
    "event_study_costnet": {"n_names", "item", "item_not", "drift_col"},
    "event_portfolio": {"n_names", "item", "item_not", "entry_lag", "exit_lag"},
}


# ---------------------------------------------------------------- registry --
def load_registry() -> dict:
    if REGISTRY.exists():
        reg = json.loads(REGISTRY.read_text())
    else:
        # seed with the hand-run history (E1: 6 configs, E9: 8 tests)
        reg = {"total_experiments": 14, "runs": []}
    reg.setdefault("families", {})
    reg.setdefault("registered_specs", {})
    reg.setdefault("research_budget", {})
    # Immutable de-dupe set: a discovery spec that has already RUN must never
    # run again, no matter what the mutable hypotheses.yaml status says (the
    # director or a human editing the file can silently reset a "done" back to
    # "pending" — re-running would re-inflate the family's Bonferroni trial
    # count, exactly the data-snooping the armor exists to prevent). Migrate
    # once from the append-only results ledger so past runs are recognized.
    if "discovery_hashes" not in reg:
        reg["discovery_hashes"] = _discovery_hashes_from_results()
    return reg


def _discovery_hashes_from_results() -> dict:
    """Reconstruct {spec_hash: result_id} for every discovery run ever recorded.
    spec_hash uses only type/params/family, so it is stable across the extra
    bookkeeping keys the ledger stores alongside a spec."""
    out: dict[str, str] = {}
    if not RESULTS.exists():
        return out
    for line in RESULTS.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        # rows predating phase-tagging (N0015-N0020) were all discovery runs;
        # only explicit confirmation rows are excluded from the dedupe set.
        # ERROR rows are excluded too: a crash observed no data, so its hash
        # must not block a retry (same rule already_run_discovery applies).
        if (row.get("phase", "discovery") == "discovery" and row.get("spec")
                and row.get("verdict") != "ERROR"):
            out[spec_hash(row["spec"])] = row.get("id")
    return out


def save_registry(reg: dict) -> None:
    REGISTRY.write_text(json.dumps(reg, indent=1))


def _family_of(spec: dict) -> str:
    return spec.get("family") or f"auto-{spec.get('type', 'unknown')}"


def spec_hash(spec: dict) -> str:
    core = {"type": spec.get("type"), "params": spec.get("params", {}),
            "family": _family_of(spec)}
    return hashlib.md5(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]


def harness_version() -> str:
    """Fingerprint the calculation, including imported maths and config.

    Hashing only harness/primitives did not notice changes to the backtest,
    Sharpe/HAC math, signal construction or cost settings. Old stamps remain
    in the append-only record; new results identify their full source inputs.
    Credentials, live state and research verdicts are deliberately excluded.
    """
    project = ROOT.parent
    paths = [ROOT / f for f in ("harness.py", "primitives.py", "h1_event_study.py")]
    paths.append(project / "config" / "config.yaml")
    for directory in ("backtest", "core", "systems"):
        paths.extend((project / directory).rglob("*.py"))
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        if path.is_file():
            digest.update(str(path.relative_to(project)).encode() + b"\0")
            digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()[:12]


def universe_hash() -> str:
    p = ROOT.parent / "data" / "universe" / "equities.json"
    return hashlib.md5(p.read_bytes()).hexdigest()[:12] if p.exists() else "seed45"


def format_error(exc: BaseException, frames: int = 3, max_len: int = 400) -> str:
    """TYPE at file.py:LINE in func <- caller.py:LINE in caller: message.
    Location-FIRST on purpose: the director/engineer context builders truncate
    error strings at 300 chars, so the frames must precede a possibly-long
    message. Innermost frame first. Falls back to 'TYPE: msg' when there is no
    traceback. Never raises."""
    try:
        tb = traceback.extract_tb(exc.__traceback__)
    except Exception:
        tb = None
    try:
        if tb:
            where = " <- ".join(f"{Path(f.filename).name}:{f.lineno} in {f.name}"
                                for f in reversed(tb[-frames:]))
            return f"{type(exc).__name__} at {where}: {exc}"[:max_len]
        return f"{type(exc).__name__}: {exc}"[:max_len]
    except Exception:
        return type(exc).__name__


# ------------------------------------------------------------- validation --
def validate_spec(spec: dict) -> str | None:
    """Return an error string, or None if the spec is admissible."""
    if spec.get("type") not in ALLOWED_PARAMS:
        return f"unknown type {spec.get('type')}"
    extra = set(spec.get("params", {})) - ALLOWED_PARAMS[spec["type"]]
    if extra:
        return f"illegal params {sorted(extra)}"
    for k in ("start", "end", "window", "lockbox"):
        if k in spec.get("params", {}) or k in spec:
            return "specs may not choose dates — windows are harness-owned"
    # Pre-registration integrity: a spec carrying a prereg hash must still BE
    # that experiment. spec_hash covers type/params/family only, so bookkeeping
    # keys (status, result_id, source, rationale, name) may change freely —
    # only the experiment itself is pinned. N0025 (2026-07-31): a prereg-only
    # yaml recovery stripped the params blocks; the default static blend ran and
    # was recorded under the ic-adaptive label, and its stripped twin hashed
    # identical to it and was dedupe-marked 'done' without ever running.
    if spec.get("prereg") and spec_hash(spec) != spec["prereg"]:
        return (f"spec no longer matches its pre-registration "
                f"(hash {spec_hash(spec)} != registered {spec['prereg']}) — "
                f"params were altered or lost after registration; a registered "
                f"spec must run exactly as registered. Re-propose it as a new "
                f"spec.")
    fam = load_registry()["families"].get(_family_of(spec))
    if fam and fam.get("status") in ("burned", "confirmed"):
        return f"family '{_family_of(spec)}' is {fam['status']} — closed to new trials"
    return None


def already_run_discovery(spec: dict) -> str | None:
    """Result id if this exact spec (by hash) has already had a discovery run
    that OBSERVED DATA, else None. Immutable — survives any rewrite of the
    mutable queue file.

    An ERROR row is not an answer: the run crashed before any metric existed,
    so zero bits were observed and re-running the identical registered spec
    inflates nothing. The verdict is consulted in the ledger rather than
    trusting the hash alone, which also unblocks legacy ERROR hashes recorded
    before this fix (e.g. N0026). The failure direction is deliberate: if the
    ledger is missing, unreadable, or the row cannot be found, the dedupe
    HOLDS — a read failure must never widen reruns.
    """
    rid = load_registry().get("discovery_hashes", {}).get(spec_hash(spec))
    if rid is None:
        return None
    try:
        for line in RESULTS.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("id") == rid:
                return None if row.get("verdict") == "ERROR" else rid
    except Exception:
        return rid          # unreadable ledger -> dedupe holds
    return rid              # row not found -> dedupe holds


def register_spec(spec: dict) -> str:
    """Pre-registration: hash+timestamp into the ledger BEFORE any run."""
    h = spec_hash(spec)
    reg = load_registry()
    if h not in reg["registered_specs"]:
        reg["registered_specs"][h] = {
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "name": spec.get("name"), "family": _family_of(spec),
            "type": spec.get("type"),
        }
        save_registry(reg)
    return h


# -------------------------------------------------------------- execution --
def execute_spec(spec: dict, window: str = "explore") -> dict:
    """Run a REGISTERED spec inside the harness-owned window."""
    from research import primitives
    h = spec_hash(spec)
    reg = load_registry()
    if h not in reg["registered_specs"]:
        raise PermissionError(f"spec {h} not registered before run")
    start, end = EXPLORE if window == "explore" else LOCKBOX
    fn = getattr(primitives, spec["type"])
    return fn(spec.get("params", {}), start=start, end=end)


# Back-compat runner map (explore window)
RUNNERS = {
    t: (lambda params, _t=t: execute_spec(
        {"type": _t, "params": params, "family": f"auto-{_t}",
         "__prereg": register_spec({"type": _t, "params": params,
                                    "family": f"auto-{_t}"})},
        window="explore"))
    for t in ALLOWED_PARAMS
}


# ---------------------------------------------------------------- verdicts --
def _event_p(t: float | None) -> float:
    if t is None or t != t:
        return 1.0
    return float(2.0 * (1.0 - norm.cdf(abs(t))))


def verdict_of(spec: dict, metrics: dict, family_trials: int = 1) -> str:
    """Discovery verdict: type bar AND within-family Bonferroni."""
    if not metrics:
        return "FAIL"
    fam_ok = True
    if spec["type"] in ("event_study", "event_study_costnet", "event_portfolio"):
        t = metrics.get("t")
        base = (t is not None) and abs(t) >= EVENT_T_BAR
        fam_ok = _event_p(t) * max(family_trials, 1) < FAMILY_ALPHA
        return "PASS" if (base and fam_ok) else "FAIL"
    if spec["type"] == "signal_backtest":
        return "PASS" if metrics.get("dsr", 0) > DSR_BAR else "FAIL"
    return "FAIL"


def confirmation_verdict(metrics: dict) -> str:
    """One-shot lockbox bar (pre-registered): t>=2.0 AND positive net
    annualized return. No multiplicity — it is a single test. For backtest
    confirmations t is derived from the lockbox-window Sharpe."""
    t = metrics.get("t")
    if t is None and metrics.get("sharpe") is not None and metrics.get("n_days"):
        t = metrics["sharpe"] * (metrics["n_days"] / 252.0) ** 0.5
    t = t or 0
    ann = metrics.get("ann_return") or metrics.get("net_mean") or 0
    return "CONFIRMED" if (t >= CONFIRM_T_BAR and ann > 0) else "BURNED"


# ----------------------------------------------------------------- ledger --
def record(spec: dict, metrics: dict | None, error: str | None,
           phase: str = "discovery") -> dict:
    reg = load_registry()
    reg["total_experiments"] += 1
    fam_name = _family_of(spec)
    fam = reg["families"].setdefault(
        fam_name, {"trials": 0, "status": "open", "confirmations_used": 0})
    if phase == "discovery":
        fam["trials"] += 1

    if metrics is None:
        verdict = "ERROR"
    elif phase == "confirmation":
        verdict = confirmation_verdict(metrics)
    else:
        verdict = verdict_of(spec, metrics, family_trials=fam["trials"])

    if phase == "discovery" and verdict == "PASS" and fam["status"] == "open":
        fam["status"] = "candidate"
        fam["candidate_spec"] = {k: spec.get(k) for k in
                                 ("name", "type", "params", "family")}
    # An ERROR spends nothing and reveals nothing: the exception happened before
    # any metric existed, so zero bits about the lockbox outcome were observed.
    # The family therefore stays 'candidate' and pending_confirmation() hands it
    # back tomorrow night; the ERROR row is still appended to RESULTS so every
    # attempt on the lockbox is visible to an auditor. (A discovery ERROR does
    # consume a trial above — by policy, counters never decrement.)
    if phase == "confirmation" and verdict != "ERROR":
        fam["confirmations_used"] += 1
        fam["status"] = "confirmed" if verdict == "CONFIRMED" else "burned"

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "id": f"N{reg['total_experiments']:04d}",
        "phase": phase, "spec": spec, "metrics": metrics, "error": error,
        "verdict": verdict, "family": fam_name, "family_trials": fam["trials"],
        "stamp": {"spec": spec_hash(spec), "harness": harness_version(),
                  "universe": universe_hash(),
                  "window": list(LOCKBOX if phase == "confirmation" else EXPLORE)},
    }
    reg["runs"] = (reg.get("runs", []) + [
        {"id": entry["id"], "type": spec.get("type"), "family": fam_name,
         "phase": phase, "verdict": verdict}])[-500:]
    # A crashed run observed no data, so its hash must not block a deliberate
    # retry of the identical registered spec; the trial above was still consumed.
    if phase == "discovery" and verdict != "ERROR":
        reg.setdefault("discovery_hashes", {})[spec_hash(spec)] = entry["id"]
    save_registry(reg)
    with open(RESULTS, "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


# ------------------------------------------------------------ confirmation --
def pending_confirmation() -> str | None:
    """The family (if any) that has earned its single lockbox shot."""
    for name, fam in load_registry()["families"].items():
        if fam.get("status") == "candidate" and fam.get("confirmations_used", 0) == 0:
            return name
    return None


def run_confirmation(family: str) -> dict:
    """Fire a family's ONE lockbox confirmation.

    The confirmation instrument is pre-registered here, not agent-chosen:
    event families confirm via the calendar-time portfolio (overlap-robust,
    cost-net, and it doubles as the sleeve's backtest); backtest families
    re-run their candidate spec on the lockbox window.
    """
    reg = load_registry()
    fam = reg["families"].get(family)
    if not fam or fam.get("status") != "candidate":
        raise PermissionError(f"family {family} has no confirmation right")
    if fam.get("confirmations_used", 0) > 0:
        raise PermissionError(f"family {family} already used its one shot")

    cand = dict(fam.get("candidate_spec") or {})
    if cand.get("type") in ("event_study", "event_study_costnet"):
        conf_spec = {"name": f"CONFIRM {family} (calendar-time, lockbox)",
                     "type": "event_portfolio", "family": family,
                     "params": {k: v for k, v in cand.get("params", {}).items()
                                if k in ALLOWED_PARAMS["event_portfolio"]}}
    else:
        conf_spec = {**cand, "name": f"CONFIRM {family} (lockbox)"}

    register_spec(conf_spec)
    metrics, error = None, None
    try:
        metrics = execute_spec(conf_spec, window="lockbox")
    except Exception as exc:
        error = format_error(exc)
    return record(conf_spec, metrics, error, phase="confirmation")
