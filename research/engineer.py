"""Staff-Engineer + quant agent lane (E22, full authority since E30/E32).

Nightly, one Fable-5 session (Opus fallback) reads the ENTIRE platform — every
source file, the research log, live ops state, the FUND's performance and the
research ledger — and emits 0-2 changes: complete replacement file contents +
rationale + risk class. Under E30 the lane APPLIES ITS OWN WORK: a passing
proposal is written to the repo and committed to git the same night, with no
human gate on any path. Zero proposals is an explicitly valid night (E32).

Mechanical guarantees (none of this relies on trusting the model):
  - path validation: no traversal, no absolute paths, .env / the research
    ledger (registry.json, RESULTS.jsonl) can never be WRITE targets — the
    agent reads results but can never edit the exam it is graded by
  - every proposed .py must ast-parse before it is even stored
  - a sandbox pytest runs each proposal in a temp copy of the repo WITHOUT
    .env and WITHOUT data/; only a green sandbox is auto-applied
  - the apply gate re-runs the FULL suite against the real repo and restores
    the backup on red, so a bad change cannot survive the night
  - one git commit per applied proposal -> granular `git revert`
  - backpressure: while >= max_pending proposals sit unapplied, no new session
  - config cage: agent_autonomy.mode=real_money_locked disables the lane

Recoverability is the whole safety model, so what is NOT recoverable is stated
to the agent in its system prompt: the lockbox one-shot, the pre-registration
clock, executed broker orders. Those it must leave to the human.

scripts/review_proposals.py remains the human's manual entry point (show /
apply / reject) for the nights it wants to intervene.
"""
from __future__ import annotations

import ast
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import CONFIG
from core.env import get_key

ROOT = Path(__file__).resolve().parent.parent
PROPOSALS_DIR = Path(__file__).resolve().parent / "proposals"
WISHES = Path(__file__).resolve().parent / "WISHES.md"
ENGINEER_MEMOS = Path(__file__).resolve().parent / "ENGINEER_MEMOS.md"

# targets that can NEVER be proposed, in any mode (defense in depth — the
# config protected_paths list is a review-time tripwire; these are absolute)
ALWAYS_FORBIDDEN = {".env", "research/registry.json", "research/RESULTS.jsonl"}
ALLOWED_EXT = {".py", ".yaml", ".md", ".txt", ".sh"}
ALLOWED_TOPDIRS = {"core", "systems", "runtime", "dashboard", "research",
                   "scripts", "tests", "backtest", "config", "deploy", "docs"}
ALLOWED_ROOT_FILES = {"RESEARCH_LOG.md", "README.md", "SPEC.md",
                      "TECHNICAL_APPENDIX.md", "requirements.txt"}
MAX_FILES_PER_PROPOSAL = 8
MAX_TOTAL_CHARS = 300_000

# sandbox copy: everything the tests need, nothing secret or heavy.
# _COPY_IGNORE matches at ANY depth (caches, secrets); _COPY_IGNORE_TOP only
# at the repo root — "data" must NOT match core/data/ (that broke every
# sandbox collection on night one: ModuleNotFoundError core.data).
_COPY_IGNORE = {"venv", "__pycache__", ".pytest_cache", ".git", ".env",
                ".claude"}
_COPY_IGNORE_TOP = {"data", "logs", "mulkusa-site"}


def _autonomy() -> dict:
    return CONFIG.get("agent_autonomy", {}) or {}


# ------------------------------------------------------------------ store --
def validate_rel_path(p: str) -> str | None:
    """Return an error string if this proposal target is inadmissible."""
    if not p or p.startswith("/") or p.startswith("\\"):
        return "absolute paths not allowed"
    parts = Path(p).parts
    if ".." in parts:
        return "path traversal not allowed"
    if p in ALWAYS_FORBIDDEN or Path(p).name == ".env":
        return "forbidden target"
    if p.startswith("research/proposals"):
        return "cannot propose into the proposals store"
    if Path(p).suffix not in ALLOWED_EXT:
        return f"extension '{Path(p).suffix}' not allowed"
    if len(parts) == 1:
        return None if p in ALLOWED_ROOT_FILES else "root file not in allowlist"
    return None if parts[0] in ALLOWED_TOPDIRS else f"'{parts[0]}/' outside allowed tree"


def list_proposals() -> list[dict]:
    out = []
    if not PROPOSALS_DIR.exists():
        return out
    for d in sorted(PROPOSALS_DIR.iterdir()):
        pj = d / "proposal.json"
        if pj.exists():
            try:
                out.append(json.loads(pj.read_text()))
            except json.JSONDecodeError:
                continue
    return out


# A proposal whose sandbox went red is not work in progress. Nothing in the
# full-authority lane will ever apply it: apply_proposal only runs on a green
# sandbox, and no human drains this queue any more. It is a corpse.
_DEAD_AFTER_DAYS = 3


def is_actionable(p: dict) -> bool:
    """Could this pending proposal still become applied code?

    E53. `pending_count` counted every pending proposal, and the backpressure
    cap refused to start the lane once that reached max_pending_proposals. That
    was correct under E22, where a human reviewed the queue each morning and
    drained it. Under E30's full authority nothing drains a FAILED proposal, so
    red sandboxes accumulate, hit the cap, and stop the lane permanently.

    It happened: on 2026-08-02 the queue held P0015 (green, awaiting review),
    plus P0019 and P0020 (both red, and both explicitly superseded by P0023,
    which had already been applied) and P0028 (red). Four pending against a cap
    of four. The night's digest said "engineer skipped: 4 proposals pending
    review (backpressure cap 4)" and the lane did nothing at all.

    Fifth time a correct mechanism has quietly stopped the work this week
    (E40 headroom, E43 family rule, E44 session cap, E45 director 400). The
    cap is right; counting corpses toward it is not.

    Raising the ceiling would be the wrong fix. The ceiling exists so unread
    work cannot pile up without bound, and that reason still holds. What has to
    change is that dead proposals stop counting as work.
    """
    if p.get("status") != "pending":
        return False
    sb = p.get("sandbox")
    # "not yet sandboxed" and "sandboxed and red" are different states, and
    # conflating them was my own bug in the first cut of this: a proposal
    # written seconds ago has no sandbox result and is very much alive. Only a
    # sandbox that RAN and failed makes it unappliable.
    if sb is not None and not sb.get("passed"):
        return False
    if p.get("superseded_by"):
        return False
    ts = p.get("created_at") or p.get("ts") or ""
    if ts:
        from datetime import datetime, timedelta, timezone
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(ts)
            if age > timedelta(days=_DEAD_AFTER_DAYS):
                return False              # nobody is coming for it
        except ValueError:
            pass
    return True


def pending_count() -> int:
    """Backpressure counts work that could still land, not everything unclosed."""
    return sum(1 for p in list_proposals() if is_actionable(p))


def retire_stale_proposals() -> list[dict]:
    """Close proposals that can never be applied, with the reason recorded.

    Runs before the backpressure check so a corpse cannot silently hold the
    lane shut. Closing is bookkeeping, not judgement: it never touches applied
    code, and the proposal, its diff and its memo stay on disk for reading.
    """
    closed = []
    for p in list_proposals():
        if p.get("status") != "pending" or is_actionable(p):
            continue
        sb = p.get("sandbox")
        why = ("sandbox red and nothing in the lane can apply it"
               if sb is not None and not sb.get("passed")
               else f"unreviewed for more than {_DEAD_AFTER_DAYS} days")
        p["status"] = "retired"
        p["retire_reason"] = why
        save_record(p)
        closed.append({"id": p.get("id"), "why": why})
    return closed


def _next_id() -> str:
    mx = 0
    for p in list_proposals():
        m = re.match(r"P(\d{4})", p.get("id", ""))
        if m:
            mx = max(mx, int(m.group(1)))
    return f"P{mx + 1:04d}"


def write_proposal(spec: dict) -> tuple[dict | None, str | None]:
    """Validate + persist one proposal. Returns (record, error)."""
    files = spec.get("files") or []
    if not files or len(files) > MAX_FILES_PER_PROPOSAL:
        return None, f"must contain 1..{MAX_FILES_PER_PROPOSAL} files"
    if sum(len(f.get("content", "")) for f in files) > MAX_TOTAL_CHARS:
        return None, "total content exceeds size cap"
    for f in files:
        err = validate_rel_path(f.get("path", ""))
        if err:
            return None, f"{f.get('path')}: {err}"
        if f["path"].endswith(".py"):
            try:
                ast.parse(f["content"])
            except SyntaxError as exc:
                return None, f"{f['path']}: syntax error — {exc}"

    pid = _next_id()
    slug = re.sub(r"[^a-z0-9]+", "-", spec.get("title", "untitled").lower())[:40].strip("-")
    pdir = PROPOSALS_DIR / f"{pid}-{slug}"
    (pdir / "files").mkdir(parents=True, exist_ok=True)

    diffs = []
    for f in files:
        target = ROOT / f["path"]
        old = target.read_text().splitlines(keepends=True) if target.exists() else []
        new = f["content"].splitlines(keepends=True)
        diffs.append("".join(difflib.unified_diff(
            old, new, fromfile=f"a/{f['path']}", tofile=f"b/{f['path']}")))
        dest = pdir / "files" / f["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f["content"])
    (pdir / "diff.patch").write_text("\n".join(diffs))
    (pdir / "MEMO.md").write_text(
        f"# {pid}: {spec.get('title')}\n\n**Kind:** {spec.get('kind')} · "
        f"**Risk:** {spec.get('risk_class')}\n\n## Rationale\n{spec.get('rationale')}\n\n"
        f"## Test note\n{spec.get('test_note', '—')}\n")

    rec = {
        "id": pid, "ts": datetime.now(timezone.utc).isoformat(),
        "title": spec.get("title"), "kind": spec.get("kind"),
        "risk_class": spec.get("risk_class"), "rationale": spec.get("rationale"),
        "test_note": spec.get("test_note"),
        "files": [{"path": f["path"],
                   "existed": (ROOT / f["path"]).exists()} for f in files],
        "status": "pending", "dir": pdir.name,
    }
    (pdir / "proposal.json").write_text(json.dumps(rec, indent=1))
    return rec, None


def save_record(rec: dict) -> None:
    (PROPOSALS_DIR / rec["dir"] / "proposal.json").write_text(json.dumps(rec, indent=1))


# ------------------------------------------------------------ apply side --
def _protected(path: str) -> bool:
    prot = _autonomy().get("protected_paths") or []
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prot)


# Paths the live trading process does NOT hold: changing them cannot be
# dormant, because nothing in the running image imported them.
_OFFLINE_PREFIXES = ("research/", "scripts/", "tests/", "backtest/", "deploy/")


def _needs_restart(path: str) -> bool:
    """Would this change sit dormant until the trading process reloads?

    E48b. This was an allow-list of runtime/, dashboard/ and core/, which
    missed two thirds of the surface the process actually holds: live.py
    imports ten modules out of systems/, and CONFIG is read from
    config/config.yaml at import time. So a lane change to the vol-scaling
    math or to any risk limit applied cleanly, committed, reported
    "APPLIED @sha", and had no effect on the running book.

    That is E33's dormant-fix gap still open on the parts that trade, and
    worse than E33 was, because now it looks closed. Under the settle loop it
    compounds: the next round rereads the tree, sees its own change on disk,
    and reasons about a system that is not the one running.

    Deny-list, not allow-list. A new top-level package is assumed to be inside
    the process until someone proves otherwise, because the safe default is a
    needless restart, not a silent no-op.
    """
    return not path.startswith(_OFFLINE_PREFIXES)


def apply_proposal(pid: str, force_armor: bool = False,
                   run_tests: bool = True) -> dict:
    """HUMAN-invoked: backup -> write -> pytest -> rollback on failure.

    Refuses protected-path targets without force_armor, and refuses everything
    without it when the cage is real_money_locked.
    """
    rec = next((p for p in list_proposals() if p["id"] == pid), None)
    if rec is None:
        return {"ok": False, "error": f"{pid} not found"}
    if rec.get("status") != "pending":
        return {"ok": False, "error": f"{pid} is '{rec.get('status')}', not pending"}
    locked = _autonomy().get("mode") == "real_money_locked"
    hot = [f["path"] for f in rec["files"] if _protected(f["path"])]
    if (hot or locked) and not force_armor:
        why = f"protected paths {hot}" if hot else "autonomy mode is real_money_locked"
        return {"ok": False, "error": f"refusing without --force-armor: {why}"}

    pdir = PROPOSALS_DIR / rec["dir"]
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backups: list[tuple[Path, Path | None]] = []

    def _rollback() -> None:
        for target, bak in backups:
            if bak is not None:
                shutil.copy2(bak, target)
            elif target.exists():
                target.unlink()

    # E48b: everything from the first byte written to the test verdict runs
    # under one rollback. It did not, and there were two live ways out of this
    # function that left the tree half-changed with nobody restoring it:
    #
    #   - an IOError partway through the write loop. Not hypothetical: the
    #     sandbox filled a tmpfs one day earlier (E46), and this loop writes
    #     into the live tree, not a sandbox.
    #   - `subprocess.TimeoutExpired` from the pytest gate. The suite runs 65s
    #     normally but E31 recorded it at 518s against a hanging Gateway, so
    #     the 900s wall is reachable. That path was the worse of the two: the
    #     tree ends up FULLY written, never verified, never rolled back.
    #
    # Both propagated out as exceptions, so the proposal also stayed "pending"
    # while its code was already live in the tree.
    try:
        for f in rec["files"]:
            target = ROOT / f["path"]
            bak = None
            if target.exists():
                bak = target.with_name(target.name + f".bak.{ts}")
                shutil.copy2(target, bak)
            backups.append((target, bak))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pdir / "files" / f["path"], target)

        if run_tests:
            r = subprocess.run(
                [sys.executable, "-m", "pytest", "tests/", "-q", *_timeout_args()],
                cwd=ROOT, capture_output=True, text=True, timeout=900)
            if r.returncode != 0:
                _rollback()                      # rollback, keep it pending
                tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-8:])
                rec["apply_note"] = f"apply FAILED tests, rolled back @{ts}: {tail[:500]}"
                save_record(rec)
                return {"ok": False, "error": "tests failed — rolled back",
                        "detail": tail}
    except Exception as exc:
        _rollback()
        why = f"{type(exc).__name__}: {exc}"
        rec["apply_note"] = f"apply ABORTED @{ts}, rolled back: {why[:400]}"
        save_record(rec)
        return {"ok": False, "error": f"apply aborted, rolled back — {why[:200]}"}

    rec["status"] = "applied"
    rec["apply_note"] = f"applied @{ts}" + ("" if run_tests else " (tests skipped)")
    save_record(rec)
    touched = [f["path"] for f in rec["files"]]
    needs_restart = any(_needs_restart(p) for p in touched)
    out = {"ok": True, "touched": touched, "needs_restart": needs_restart,
           "note": "sync local<->server copies manually" if needs_restart else ""}
    # E34: an applied fix touching runtime/core/dashboard is inert until the
    # process reloads — P0011 sat dormant 8h (E33). With auto_restart the lane
    # closes its own loop. Fail-soft: a restart failure never un-applies the
    # change; the E33 restart-drift pager remains the independent safety net.
    if needs_restart and _autonomy().get("auto_restart"):
        out["restarted"] = _restart_service()
        rec["apply_note"] += (" · restarted" if out["restarted"].get("ok")
                              else " · RESTART FAILED")
        save_record(rec)
    return out


def _restart_service(unit: str = "paper-trader") -> dict:
    """Restart the live service via the narrow sudoers rule (restart-only,
    exact unit). Verifies the unit came back before claiming success."""
    import time
    try:
        r = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "restart", unit],
                           capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            return {"ok": False, "unit": unit,
                    "error": (r.stdout + r.stderr).strip()[:200] or "sudo denied"}
        time.sleep(8)
        chk = subprocess.run(["systemctl", "is-active", unit],
                             capture_output=True, text=True, timeout=15)
        state = chk.stdout.strip()
        return ({"ok": True, "unit": unit} if state == "active"
                else {"ok": False, "unit": unit, "error": f"unit state: {state}"})
    except Exception as exc:
        return {"ok": False, "unit": unit, "error": f"{type(exc).__name__}: {exc}"}


def git_commit(paths: list[str], message: str) -> dict:
    """Commit applied files so every auto-apply is its own revertible point.

    Best-effort: a git failure must never fail an otherwise-good apply, it just
    means that proposal shares the next commit instead of owning one.
    """
    try:
        subprocess.run(["git", "add", "--"] + paths, cwd=ROOT,
                       capture_output=True, text=True, timeout=60, check=True)
        r = subprocess.run(["git", "commit", "-m", message], cwd=ROOT,
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return {"ok": False, "error": (r.stdout + r.stderr).strip()[:300]}
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return {"ok": True, "sha": sha}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def reject_proposal(pid: str, reason: str) -> dict:
    rec = next((p for p in list_proposals() if p["id"] == pid), None)
    if rec is None:
        return {"ok": False, "error": f"{pid} not found"}
    if rec.get("status") != "pending":
        return {"ok": False, "error": f"{pid} is '{rec.get('status')}', not pending"}
    rec["status"] = "rejected"
    rec["reject_reason"] = reason
    save_record(rec)
    return {"ok": True}


# ---------------------------------------------------------------- sandbox --
def _sandbox_tmp_root() -> str | None:
    """A DISK-backed directory for the sandbox, not a RAM-backed one (E46).

    The server's /tmp is a 1.9GB tmpfs. A sandbox copy plus the pytest
    basetemp inside it is hundreds of MB, and two proposals a night plus the
    apply gate's own run can exhaust it — which surfaces as dozens of
    unrelated test ERRORs and silently discards the night's work. /var/tmp is
    conventionally disk-backed; fall back to the default if it is missing or
    unwritable so this can never be the thing that breaks the sandbox.
    """
    for cand in ("/var/tmp",):
        if os.path.isdir(cand) and os.access(cand, os.W_OK):
            return cand
    return None


def _copy_repo(dst: Path) -> None:
    src_root = Path(ROOT).resolve()

    def ignore(d, names):
        at_root = Path(d).resolve() == src_root
        under_research = Path(d).name == "research"
        out = []
        for n in names:
            full = Path(d) / n
            # skip anything the current user cannot read/traverse — a single
            # stray root-owned file must never crash the whole sandbox copy
            # (and thus the nightly engineer session). os.access with the real
            # uid is exactly the sandbox's own read capability.
            unreadable = not os.access(full, os.R_OK) or (
                full.is_dir() and not os.access(full, os.X_OK))
            if (n in _COPY_IGNORE or ".bak" in n or n.endswith(".pkl")
                    # E46: logs, at ANY depth. _COPY_IGNORE_TOP excluded the
                    # logs/ DIRECTORY, but stale runtime logs sat in the repo
                    # ROOT — 271MB of them — and every sandbox copied all of it
                    # into a 1.9GB tmpfs. Two nights of the lane's work were
                    # discarded on ENOSPC that looked like 51 test errors.
                    # No test has ever needed a log file.
                    or n.endswith(".log") or ".log." in n
                    or unreadable
                    or (at_root and n in _COPY_IGNORE_TOP)
                    or (under_research and n == "proposals")):
                out.append(n)
        return out

    shutil.copytree(ROOT, dst, ignore=ignore, dirs_exist_ok=True)
    # the frozen universe file is code-adjacent config, not runtime state —
    # tests import universe.py which may read it; state.json stays excluded
    uni = src_root / "data" / "universe"
    if uni.exists():
        shutil.copytree(uni, dst / "data" / "universe", dirs_exist_ok=True)


def sandbox_test(pdir: Path, timeout: int = 600) -> dict:
    """Apply the proposal in a temp copy (no .env, no data/) and run pytest.

    Two failure modes were papered over one at a time before E31 and are now
    fixed at the root, because E30 auto-apply keys on `passed` — a flaky ❌ no
    longer just asks a human to look, it silently strands a good proposal:

      * No per-test wall. `pytest-timeout` was never installed, so the old
        `--timeout 120` was dead code behind a silent guard; a single wedged
        test (e.g. a real socket connect on its 25s cap, stacked under load)
        ran until the whole-run wall and lost the entire session's signal.
        `_timeout_args()` now caps each test; a hang fails THAT test, named and
        actionable, instead of the run.
      * Live-loop contention. The nightly runs at 04:30 while a rebalance tick
        can pin a core on this 2-vCPU box, stretching the ~82s idle suite well
        past the old 420s wall on a bad overlap. With per-test caps in place
        the whole-run wall is raised to 600s to absorb contention safely — the
        per-test cap, not the wall, is the real guard against a true hang.
    """
    infra = None if _has_pytest_timeout() else (
        "INFRA: pytest-timeout missing — per-test wall inactive; `pip install "
        "pytest-timeout` (it is in requirements.txt)")
    with tempfile.TemporaryDirectory(prefix="proposal_sbx_",
                                     dir=_sandbox_tmp_root()) as tmp:
        tmp_root = Path(tmp) / "repo"
        _copy_repo(tmp_root)
        for f in (pdir / "files").rglob("*"):
            if f.is_file():
                rel = f.relative_to(pdir / "files")
                dest = tmp_root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
        try:
            # --basetemp inside the ephemeral dir: shared /tmp/pytest-of-*
            # accumulates numbered run dirs across sessions and its teardown
            # scan can hit EMFILE (observed night one: P0002 flake-failed in a
            # dirty /tmp, passed 162/162 in a clean one)
            base = ["-q", "-p", "no:cacheprovider",
                    f"--basetemp={Path(tmp) / 'pytest-tmp'}", *_timeout_args()]
            r = subprocess.run(
                [sys.executable, "-m", "pytest", "tests/", *base],
                cwd=tmp_root, capture_output=True, text=True, timeout=timeout)
            tail = (r.stdout + r.stderr).strip().splitlines()[-6:]
            summary = "\n".join(([infra] if infra else []) + tail)
            return {"passed": r.returncode == 0, "summary": summary}
        except subprocess.TimeoutExpired:
            # whole-run wall hit despite per-test caps: contention, not a hang
            note = (" (per-test wall active — likely resource contention, "
                    "not a single hung test)" if not infra else f" — {infra}")
            return {"passed": False,
                    "summary": f"sandbox pytest timed out ({timeout}s){note}"}
        except Exception as exc:
            return {"passed": False, "summary": f"{type(exc).__name__}: {exc}"}


# One wedged test must never consume the sandbox/apply budget. Signal method
# (SIGALRM, main thread, Unix) can interrupt a blocked syscall such as a hung
# socket connect — the exact P0009-class failure — where the thread method
# cannot. 90s is comfortably above the slowest healthy test yet well under any
# per-test share of the wall.
PER_TEST_TIMEOUT_S = 90


def _has_pytest_timeout() -> bool:
    try:
        import pytest_timeout  # noqa: F401
        return True
    except ImportError:
        return False


def _timeout_args() -> list[str]:
    """Per-test wall flags, shared by the sandbox and the apply gate. Empty if
    pytest-timeout is somehow absent, so the run still completes (unprotected)
    rather than erroring — sandbox_test surfaces that gap loudly in its note."""
    if _has_pytest_timeout():
        return [f"--timeout={PER_TEST_TIMEOUT_S}", "--timeout-method=signal"]
    return []


# ---------------------------------------------------------------- context --
_CTX_DIRS = ["runtime", "core", "systems", "research", "backtest", "dashboard",
             "scripts", "config", "tests"]
# 2026-08-03: 60_000 cut the planner's two hottest files out of its own view of
# the tree — runtime/live.py (79,053 chars: tick() and the mirror session gate)
# and research/engineer.py (83,759: _run_pipeline) — for four consecutive
# nights, and no honest spec can be written against code that arrives with its
# hot region missing (the realized-vol instrument wiring slipped every one of
# those nights for exactly this reason). 100_000 covers both with headroom.
# The engineers' brief is a stricter guarantee and stays separate: _gen_user
# REFUSES a spec it cannot hand over whole rather than truncating it (E42).
_MAX_FILE_CHARS = 100_000
# Append-only logs inside the walked tree. RESEARCH_LOG is tailed explicitly
# above, but these grow by a memo every night and were being included WHOLE —
# 56KB of mostly-old memos by 2026-07-28, which pushed the context to 92% of
# cap and made the headroom guard fail the suite, rolling back an approved,
# sandbox-green proposal (P0014). The lane needs its RECENT history, not its
# archive; everything older is in git and in RESEARCH_LOG.
_TAIL_CHARS = {
    "research/ENGINEER_MEMOS.md": 16_000,     # ~3-4 nights
    "research/NIGHTLY_MEMOS.md": 10_000,
    "research/WISHES.md": 6_000,
}
# E32: 600_000 left the tree at 99.6% of budget (576KB of source + meta), so
# the FIRST file the lane added — P0010's new test — silently pushed another
# test out of context. A cap that tight makes the lane's blind spot a function
# of how much it has written lately. 780_000 restores ~25% growth headroom;
# the guard test fails loudly if the tree ever outgrows it again.
# E40: 1.0M chars ~ 250K tokens, comfortably inside Fable's 1M-token window and
# ~2 months of growth headroom at the current rate (the 780K cap was hit twice
# in two days). Cost of the extra room on the one full-context call: ~$0.5/night.
# 2026-08-03: 1.2M absorbs the ~45k of newly-included content the
# _MAX_FILE_CHARS raise adds (the two hot files' tails), so the extra room goes
# to the files it was raised for rather than evicting something else. The
# no-eviction assert in tests/test_engineer_context.py stays the loud failure
# if the tree ever outgrows this.
# 2026-09-19: the current source tree exceeds 1.2M and evicts watchdog tests.
# Preserve the full reviewed tree with modest headroom; this does not enable
# the disabled engineer lane or issue any model call.
_MAX_CTX_CHARS = 1_350_000


def _curve_stats(series: list) -> dict | None:
    """Total return, max drawdown and recent move for one book's equity curve."""
    pts = [(p[1] if isinstance(p, (list, tuple)) else p) for p in series
           if p is not None]
    pts = [float(x) for x in pts if isinstance(x, (int, float))]
    if len(pts) < 2:
        return None
    peak, mdd = pts[0], 0.0
    for x in pts:
        peak = max(peak, x)
        mdd = min(mdd, x / peak - 1.0 if peak else 0.0)
    recent = pts[-min(len(pts), 48):]
    return {"marks": len(pts), "start": round(pts[0], 2), "now": round(pts[-1], 2),
            "total_ret_pct": round((pts[-1] / pts[0] - 1) * 100, 2),
            "max_dd_pct": round(mdd * 100, 2),
            "last48_ret_pct": round((recent[-1] / recent[0] - 1) * 100, 2)}


def _quant_view(s: dict) -> str:
    """The FUND's state as a quant would read it, not as an ops dashboard.

    E32: this used to be absent entirely — the lane could see every line of
    source but not one number about whether the books were making money, so
    'think like a quant' was structurally impossible, not merely un-prompted.
    """
    books = {}
    for b, curve in sorted((s.get("equity_history") or {}).items()):
        st = _curve_stats(curve or [])
        if st:
            books[b] = st
    live = {b: {"equity": round(d.get("equity", 0), 2),
                "n_positions": len(d.get("weights") or {}),
                "gross": round(sum(abs(w) for w in (d.get("weights") or {}).values()), 3)}
            for b, d in (s.get("systems") or {}).items()}
    ib = s.get("ibkr") or {}
    attrib = s.get("s4_attribution") or []
    return "=== FUND PERFORMANCE (the quant view) ===\n" + json.dumps({
        "books_since_inception": books,
        "books_now": live,
        "s5_stats": s.get("s5_stats"),
        "benchmark": _curve_stats(s.get("benchmark") or []),
        "regime": s.get("regime"),
        # E48e/E59b fair-race instrument: written every tick, previously read
        # by nothing agent-facing — realized vol per book vs the shared target
        "realized_vol_vs_target": s.get("realized_vol"),
        "data_provider": s.get("data_provider"),
        "s3_vs_s1_PREREGISTERED_RULE": s.get("s3_vs_s1"),
        "s3_vs_s1_common_DIAGNOSTIC_ONLY": s.get("s3_vs_s1_common"),
        # [symbol, weight_delta, return, pnl_usd] — biggest movers both ways
        "s4_attribution_top": sorted(
            [a for a in attrib if isinstance(a, (list, tuple)) and len(a) >= 4],
            key=lambda a: -abs(a[3] or 0))[:12],
        "s4_attribution_n": len(attrib),
        "cost_inputs_n_symbols": len(s.get("cost_inputs") or {}),
        "cost_paid_usd_cum": s.get("cost_paid_usd"),
        "turnover_l1_cum": s.get("turnover_l1_cum"),
        "last_rebalance": s.get("last_rebalance"),
        "last_rebalance_reason": s.get("last_rebalance_reason"),
        "ibkr": {k: ib.get(k) for k in
                 ("account", "mode", "nav", "n_positions", "last_sync",
                  "last_refresh", "orders_placed", "n_fills_today",
                  "refresh_error", "refresh_error_ts")},
        "ibkr_failed_orders": (ib.get("failed") or [])[:5],
        "ibkr_api_errors": (ib.get("api_errors") or [])[:5],
    }, indent=1, default=str)


def _past_proposals(proposals: list[dict]) -> list[dict]:
    """The lane's own outcomes, as a planner needs to read them.

    The section is titled "learn from outcomes" and carried none: the sandbox
    summary, the reviewer's verdict and the retire reason are all written to
    proposal.json and were all stripped here. On 2026-08-01/02 six consecutive
    proposals died — the causes sat on disk, unreadable from this context —
    and every following planner re-specced blind, turning one night of infra
    trouble into a three-night stall.

    Pure and bounded: records in, rows out, no I/O. The pytest TAIL (not the
    head) is the part that names the failing tests, 300 chars per review field
    is enough to act on. A record with none of these serializes exactly as it
    did before.
    """
    rows = []
    for p in proposals:
        row = {"id": p["id"], "title": p.get("title"), "status": p.get("status"),
               "reject_reason": p.get("reject_reason"),
               "apply_note": p.get("apply_note")}
        sb = p.get("sandbox")
        if sb is not None and not sb.get("passed"):
            row["sandbox_failed"] = str(sb.get("summary") or "")[-400:]
        rv = p.get("review")
        if isinstance(rv, dict):
            sub = {k: str(rv[k])[:300]
                   for k in ("verdict", "reasons", "revision_instructions",
                             "error") if rv.get(k)}
            if sub:
                row["review"] = sub
        if p.get("retire_reason"):
            row["retire_reason"] = p["retire_reason"]
        rows.append(row)
    return rows


def _research_ledger() -> str:
    """Experiment results + family/trial state. Not reachable by the source
    walk (both are .json, deliberately outside ALLOWED_EXT so they can never
    be proposal targets) — but READING them is what makes research judgment
    possible at all."""
    out = []
    res = ROOT / "research" / "RESULTS.jsonl"
    if res.exists():
        rows = []
        for line in res.read_text().splitlines()[-30:]:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            row = {"id": r.get("id"), "name": (r.get("spec") or {}).get("name"),
                   "family": r.get("family"), "phase": r.get("phase"),
                   "verdict": r.get("verdict"), "metrics": r.get("metrics")}
            # an ERROR verdict without its cause is unactionable, and the cause
            # is right there in the row — the engineer-side twin of the same
            # defect on the director side. Only carried when there is one, so
            # healthy rows serialize exactly as before.
            if r.get("error"):
                row["error"] = str(r.get("error"))[:300]
            rows.append(row)
        out.append("=== RESEARCH RESULTS (append-only ledger, read-only to you) ===\n"
                   + json.dumps(rows, indent=1, default=str))
    reg = ROOT / "research" / "registry.json"
    if reg.exists():
        try:
            r = json.loads(reg.read_text())
            out.append("=== FAMILIES / GLOBAL N (multiple-testing state) ===\n"
                       + json.dumps({"total_experiments": r.get("total_experiments"),
                                     "families": r.get("families")},
                                    indent=1, default=str))
        except json.JSONDecodeError:
            pass
    # E43: is the research engine actually running? It produced NOTHING for 13
    # days while every attention went to the mirror, and no instrument said so.
    try:
        from research.director import research_liveness
        out.append("=== RESEARCH LIVENESS (is the fund still asking questions?) ===\n"
                   + json.dumps(research_liveness(), indent=1, default=str))
    except Exception as exc:
        out.append(f"=== RESEARCH LIVENESS === unavailable: "
                   f"{type(exc).__name__}: {exc}")
    # The confirmed family is closed to trials forever, so its forward paper
    # record against the lockbox expectation is the ONLY evidence left on the
    # fund's one confirmed edge — diagnostic, never a decision rule.
    try:
        from research.director import forward_oos
        out.append("=== FORWARD OOS (confirmed families — diagnostic only) ===\n"
                   + json.dumps(forward_oos(), indent=1, default=str))
    except Exception as exc:
        out.append(f"=== FORWARD OOS === unavailable: "
                   f"{type(exc).__name__}: {exc}")
    return "\n\n".join(out)


def _mirror_view(ledger_path=None, state_path=None) -> str:
    """Per-session mirror reconciliation, straight off the fills ledger (E38).

    P0014 built scripts/mirror_reconcile.py — the instrument that found E36
    (83% of fills missing from the ledger) and E37 (a $20.25M-gross session on
    a $979k NAV) — but nothing piped its numbers here, so the nightly review
    could only ever say "confirm the fix tomorrow". Gross vs net notional, the
    round-trip fraction, gross/NAV and the CHURN flags are now data the planner
    reads every night.

    At the 04:30 UTC session the capture check will usually report
    comparable=false: the broker's n_fills_today counter belongs to the NEW
    session while the newest ledger day is yesterday. That is expected, not a
    defect — the gross/net table is the primary signal.

    Read-only and fail-safe by construction: no writes anywhere, and any
    failure (missing script, corrupt ledger, API drift) returns "" rather than
    taking the nightly session down over an observational section.
    """
    ledger_path = Path(ledger_path) if ledger_path else (
        ROOT / "data" / "fills_history.jsonl")
    state_path = Path(state_path) if state_path else ROOT / "data" / "state.json"
    if not ledger_path.exists():
        return ""
    try:
        # lazy, in-function load: a missing/broken script degrades to ""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "mirror_reconcile", ROOT / "scripts" / "mirror_reconcile.py")
        mr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mr)

        fills = mr.load_ledger(ledger_path)
        if not fills:
            return ""
        summary = mr.summarize(fills, mr.load_state(state_path))

        sessions = []
        for iso in summary["days_desc"][:7]:
            d = summary["days"][iso]
            gn = d.get("gross_over_nav")
            row = {"date": iso, "n_execs": d["n_execs"],
                   "gross_usd": round(d["gross_notional"]),
                   "net_usd": round(d["net_notional"]),
                   "rt_frac": round(d["round_trip_frac"], 3),
                   "gross_over_nav": round(gn, 2) if gn is not None else None,
                   "flags": d["flags"]}
            if d["flags"]:
                # per-symbol detail only where something is wrong — a clean
                # night stays a few hundred bytes of context
                row["top_churn"] = [[r[0], round(r[1]), round(r[2]), r[3]]
                                    for r in (d.get("top_churn") or [])[:3]]
            sessions.append(row)

        return ("=== MIRROR RECONCILIATION (per session, from "
                "fills_history.jsonl — E37 instrument) ===\n"
                + json.dumps({"sessions": sessions,
                              "capture_check_newest_day": summary["capture"],
                              "newest_day": summary["newest_day"],
                              "n_fills_ledger": summary["n_fills"]},
                             indent=1, default=str))
    except Exception:
        return ""


def build_context() -> str:
    parts: list[str] = []

    log = ROOT / "RESEARCH_LOG.md"
    if log.exists():
        parts.append("=== RESEARCH_LOG (tail) ===\n"
                     + "\n".join(log.read_text().splitlines()[-200:]))

    state_p = ROOT / "data" / "state.json"
    if state_p.exists():
        try:
            s = json.loads(state_p.read_text())
            parts.append("=== LIVE OPS STATE (summary) ===\n" + json.dumps({
                "ticks": s.get("ticks"), "last_tick": s.get("last_tick"),
                "tick_stats": s.get("tick_stats"),
                "data_incidents_recent": (s.get("data_incidents") or [])[-5:],
                "s3_pm_counts": s.get("s3_pm_counts"),
                "llm_budget": s.get("llm_budget"),
                "s3_vs_s1": s.get("s3_vs_s1"),
                "halt_latched": s.get("halt_latched"),
            }, indent=1))
            parts.append(_quant_view(s))
        except Exception:
            pass

    ledger = _research_ledger()
    if ledger:
        parts.append(ledger)

    # E38: E37 was verified only by a human running scripts/mirror_reconcile.py
    # by hand, and three nightly memos then deferred "confirm the fix tomorrow"
    # because this context could not see one number about it. Reconciliation
    # belongs in the nightly review as data, not as a standing memo item.
    mirror = _mirror_view()
    if mirror:
        parts.append(mirror)

    if WISHES.exists():
        parts.append("=== DIRECTOR ENGINEERING WISHES ===\n"
                     + "\n".join(WISHES.read_text().splitlines()[-60:]))

    hist = _past_proposals(list_proposals())
    if hist:
        parts.append("=== YOUR PAST PROPOSALS (learn from outcomes) ===\n"
                     + json.dumps(hist[-15:], indent=1))

    src: list[str] = []
    for d in _CTX_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for f in sorted(base.rglob("*")):
            if (f.is_dir() or f.suffix not in ALLOWED_EXT
                    or any(part in _COPY_IGNORE for part in f.parts)
                    # E32: the proposals store holds FULL COPIES of files from
                    # past proposals (728KB, 30 blobs). They are duplicates of
                    # code already in this context — and they were crowding out
                    # config/config.yaml, every script and EVERY test file. The
                    # outcome of each past proposal is already summarized in the
                    # PAST PROPOSALS section, which is the part worth reading.
                    or "proposals" in f.parts):
                continue
            try:
                text = f.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            rel = f.relative_to(ROOT)
            tail = _TAIL_CHARS.get(str(rel))
            if tail and len(text) > tail:
                # tail, not head: the newest entry is the one that matters
                text = (f"... [older {len(text) - tail} chars omitted — "
                        f"append-only log, full history in git]\n" + text[-tail:])
            elif len(text) > _MAX_FILE_CHARS:
                # E42: the marker used to report len(text) — the file's TOTAL
                # size — which read as "N chars were cut" and understated the
                # loss by an order of magnitude. Report what was actually cut.
                text = (text[:_MAX_FILE_CHARS]
                        + f"\n... [{len(text) - _MAX_FILE_CHARS:,} chars CUT of "
                          f"{len(text):,} — this file is TRUNCATED]")
            blob = f"\n--- FILE: {rel} ---\n{text}"
            src.append(blob)
    parts.append("=== SOURCE TREE ===" + "".join(src))
    context = "\n\n".join(parts)
    if len(context) > _MAX_CTX_CHARS:
        raise ValueError(
            f"Engineer context exceeds budget ({len(context):,} > "
            f"{_MAX_CTX_CHARS:,}); review context policy before a model call. "
            "No source files were silently omitted.")
    return context


# ---------------------------------------------------------------- session --
_MANDATE = """You are the head of platform AND a working quant at a systematic
paper-trading fund. Each night you read the ENTIRE codebase, the live fund
state, the performance of the four books, and the research ledger, and you
decide what — if anything — should change.

YOUR AUTHORITY IS REAL. This is not a proposal-to-a-human lane any more.
Anything you emit that passes the full test suite is applied to the live
repository automatically and committed to git, tonight, with no human in the
loop. Nobody reviews the diff in the morning. There is no protected path:
the research armor, the risk governor, the cost model and the broker layer are
all writable by you. Act like someone whose changes ship, because they do.

WHAT ROLLS BACK AND WHAT DOES NOT. A red test suite auto-reverts your change,
every applied proposal is its own git commit, and a change touching
runtime/core/dashboard is auto-restarted into the live service — so ordinary
code mistakes are cheap and recoverable. These are NOT recoverable and no test
will catch them — treat each as a one-way door:
  - the LOCKBOX one-shot: spending a family's confirmation shot burns that
    family permanently; git can restore a file, never the fact that held-out
    data was seen.
  - the pre-registration clock: changing live S3 config or the universe resets
    the 60-day decision clock (currently running).
  - executed broker orders: a filled IBKR order is filled.
  - destroying research history: RESULTS.jsonl / registry.json are the ledger
    you are graded by. They are mechanically unwritable by you, by design —
    an agent that can edit the exam it is graded by produces numbers worth
    nothing. This one stays closed at any authority level.
THE OWNER HAS DELEGATED OWNER-LEVEL AUTHORITY (2026-07-26): you MAY walk
through a one-way door, but only with a written case in your memo that would
survive a hostile future auditor — name exactly what is irreversibly spent and
why the evidence justifies spending it tonight rather than at the next
readout. When the case is not overwhelming, flagging it for the human remains
the strong move, not a failure. Casual spending of irreversible evidence is
the one unforgivable act in this job.

INVARIANTS you must never weaken (touching these means risk_class="critical"
and saying so loudly in the memo): S1 stays LLM-free (control group); the LLM
never sends orders directly (risk wrapper); all books share one ex-ante vol
normalization; pre-registered decision rules are never relaxed post-hoc;
degraded data must never trade; state schema changes are additive; costs stay
pessimistic; validation is out-of-sample only; the research armor
(pre-registration, lockbox, family Bonferroni) stays mechanical.

DOING NOTHING IS A FIRST-CLASS OUTCOME. You are not paid by the proposal. If
the fund is healthy, the backlog is genuinely low-value tonight, and you have
no idea that clears the quality bar, then emit ZERO proposals and say so:
"everything is in order; here is what I checked, here is what I am watching,
I will reassess tomorrow." A night with a sharp memo and no code is a good
night. Shipping a mediocre change to look productive is the actual failure —
it burns review budget, adds risk, and pollutes the git history. Historically
this lane shipped exactly one proposal every single night; that pattern was an
artifact of how it was prompted, not a finding about the codebase.

THINK LIKE A QUANT, NOT ONLY LIKE AN ENGINEER. You now see the books' returns,
drawdowns, attribution, regime, costs and the experiment ledger. Use them.
Beyond fixing code, you are expected to reason about the FUND: is a book
underperforming for a structural reason nobody has named? Is a cost assumption
contradicted by realized fills? Is an experiment family drifting toward a
conclusion the design cannot actually support? Is there a hypothesis nobody
has thought to test, or a measurement the platform cannot currently make?
Out-of-box ideas are explicitly wanted — including ones that question design
decisions already in RESEARCH_LOG. Argue them in the memo. If an idea needs a
new capability, build the capability; if it needs a human decision (money,
data purchase, a one-way door), make the case and leave it.

THE STANDING ORDER: FINISH THINGS. The owner's instruction, verbatim, is that
they do not want to deal with a new problem every day. Read the week that
produced it. E36: a fills cap was "fixed" in P0011 by raising 25 to 300 — the
shape was wrong, so 83% of the data was still lost and nobody noticed for
days. E37: the mirror stacked duplicate orders; the fix stopped the stacking
but not the underlying mistake, so E39 found 84% round-trip still there. E38:
a power shipped without bounding its blast radius restarted the live Gateway
from the test suite. Every one of these was a PARTIAL fix or an UNVERIFIED
one, and each bought exactly one more day before resurfacing.

So the bar is no longer "ship a good change". It is:

  1. FIX THE CLASS, NOT THE INSTANCE. Before you call something fixed, ask
     what family of defects it belongs to and whether the same shape exists
     elsewhere. A per-read cap was wrong for fills; are there other per-read
     windows on unbounded data? A test reached a live system; what else can
     the suite reach? Sweep the family in the same night.
  2. VERIFY AGAINST REAL DATA, NOT AGAINST YOUR REASONING. A green suite
     proves the code does what you wrote, not that the defect is gone. Your
     context carries the live state, the fills ledger reconciliation, the
     research ledger and the books' P&L. Before proposing anything new, spend
     the first part of your thinking checking whether YESTERDAY's fixes
     actually moved the numbers they were supposed to move. If one did not,
     that is tonight's work — finishing it beats starting something else.
  3. NAME THE FALSIFICATION TEST AND MAKE IT MECHANICAL. If your change is
     correct, some number changes. Say which, say by how much, and where
     possible ship the code that measures it so the next session sees the
     answer instead of re-deriving it. "Confirm tomorrow" in a memo is how
     three consecutive nights failed to close E37.
  4. BOUND THE BLAST RADIUS OF ANYTHING NEW. If a change gives the platform a
     new power — writing files, calling a service, spending money, touching
     the host — enumerate every context that power can now fire in, including
     the test suite and the sandbox, and close the ones that should not.
     E38's root cause was skipping this step.

You have the budget to do this properly: several parallel implementing agents,
maximum reasoning effort, and no requirement to be brief. Depth per night is
what is being bought — not more features per night. Shipping one complete,
swept, verified fix is worth more than three partial ones, and far more than a
new capability layered on an unverified base.

PRIORITIES when you do act, in order: (1) correctness bugs in live
marking/accounting/risk, (2) things the fund cannot currently MEASURE but
should, (3) the deferred backlog in RESEARCH_LOG, (4) missing tests for
untested behavior, (5) ops/monitoring gaps, (6) new research primitives that
grow the experiment grammar, (7) small refactors ONLY when they reduce risk.

Your memo is the durable artifact of the night's thinking: write it for the
quant who reads it tomorrow, not as a status report."""

# Legacy single-call output spec (pre-E34 path, kept behind engineer_pipeline
# flag): Fable emits complete file contents itself.
_SYSTEM = _MANDATE + """

QUALITY BAR: at most {max_p} proposals; zero and one are both good answers,
two is rare. Each file must be COMPLETE, runnable content (it is written to
disk verbatim — no placeholders, no '...'). Keep diffs minimal and focused;
match the existing code style (comment density, naming). When you change
behavior, update or add tests in the same proposal — your tests are now the
only thing standing between your change and the live fund. In test_note, state
how the change can be verified and what you would watch to know it went wrong."""

# E34 pipeline stage 1: the planner. Emits SPECS, not file contents — an
# implementing model (Claude Opus 5, the strongest coder at half Fable's token
# price) writes the code, and the plan comes back to Fable for review.
_PLAN_SYSTEM = _MANDATE + """

TONIGHT YOU ARE THE PLANNER of a three-stage pipeline, and the only stage that
sees the whole system. Do NOT emit file contents. Emit 0-{max_p} change SPECS.
Each is implemented IN PARALLEL by its own Claude Opus 5 engineer, which sees
ONLY the spec and the current contents of the files you name — nothing else of
the repo, and nothing of each other. The sandbox then runs the full suite on
each, and every diff comes back to you for adversarial review before it can
auto-apply. Zero specs is a first-class outcome; say why in the memo.

YOU ARE THE DECISION MAKER HERE (owner instruction, 2026-08-02). The head
quant has been told to stop directing you, and WISHES no longer carries a
reading list or a priority order. Nobody hands you the next task. You decide
what this project needs, and you are expected to disagree with the existing
conclusions rather than inherit them - several were wrong, and the ones written
most confidently were not the exceptions.

DISCOVER THE SYSTEM RATHER THAN TRUSTING ITS DESCRIPTION. The comments, the
research log and the memos tell you what somebody BELIEVED at the time they
wrote it. Two of the worst defects found this week were sitting under a comment
that described them accurately and closed the file anyway: `enabled` flags that
nothing read, and armor bars that no test pinned. When a comment says a thing
is handled, that is a place to look, not a place to skip.

The record of what has broken is long and it is evidence, not history. Read it
as data about where THIS system fails, and expect the same classes to still be
open somewhere you have not looked.

WHAT ACTUALLY CONSTRAINS YOU is mechanical and it is the owner's
pre-registration, not anyone's opinion: the statistical bars, the lockbox
window, the 60-day rule and the vol target are pinned as literals in
tests/test_preregistered_constants.py; the graded ledgers cannot be written at
any authority level; every change runs the full suite and is rolled back on
red. If you believe one of those numbers is wrong, argue it with evidence and
leave it in place. Routing around a pre-registration is not a fix, it is how a
result stops meaning anything.

The budget is open. Use the rounds. Prefer one complete, verified, class-wide
fix over three partial ones, and prefer finding the defect nobody has named yet
over polishing one that already has a test.

YOU WILL BE INVITED BACK. Tonight runs in ROUNDS, not one shot. When your work
applies, you are called again with the tree rebuilt from disk, so the next
round SEES YOUR OWN COMMITS and can build on them. Plan accordingly:

  - Do not cram a whole roadmap into round one. Send the most valuable
    COMPLETE change you can specify now. The follow-on is a later round's job,
    and it will be specified better once the first change is real code you can
    read rather than code you imagined.
  - Do not pad with filler to look busy. Rounds are cheap; bad changes are not.
  - Do not stop early out of politeness. If something in this system is wrong,
    unmeasured, or silently failing, keep working. The night ends when the work
    is done, not when you have produced one thing.
  - Ending with zero specs is how you say "everything worth doing is done, we
    wait and see". That is a real verdict and a good one when it is true. Say
    it plainly in the memo, and say what you would watch for tomorrow. Do not
    say it to avoid effort, and do not say it while a known defect is open.

BECAUSE THE ENGINEERS ARE BLIND TO EVERYTHING YOU DO NOT HAND THEM, the spec
is the entire job. A spec that omits a caller, a config key, a test file or an
invariant produces a change that is locally correct and globally wrong — and
that is precisely how partial fixes have been shipping. For each spec:

  - files_to_touch must list EVERY file the change requires, including tests
    and every caller you expect to need updating. The engineer is mechanically
    forbidden from writing anything else, so an omission cannot be recovered
    later in the night — it becomes a broken or half-applied change.
  - instructions must be complete enough that a competent engineer who has
    never seen this repo produces the right diff: what to change, where, the
    desired behavior, the edge cases, the invariants it must not weaken, and
    the failure modes to handle. Quote exact identifiers and paths.
  - acceptance states what YOU will check in the diff when it returns, and
    which live number should move if the change is right.

Specs must be INDEPENDENT: two engineers running concurrently cannot see each
other's work, so never let two specs touch the same file or depend on one
another's output. If a change is genuinely two-stage, ship stage one tonight
and say so.

QUALITY BAR: prefer one complete, family-sweeping, verified fix over several
partial ones. Use the parallelism for work that is genuinely independent —
e.g. a fix plus the instrument that proves it, or two unrelated defect
families — not to inflate the night's count."""

_GEN_SYSTEM = """You are a senior engineer implementing one reviewed change on
a live systematic paper-trading fund. You receive a single change spec and the
CURRENT contents of every file you are permitted to write. You see nothing
else of the repository, and other engineers are implementing other specs
concurrently — so treat your spec as the complete statement of the work.

Emit complete replacement file contents. They are written to disk verbatim: no
placeholders, no elisions, no '...'. What passes adversarial review and the
full test suite is committed to the LIVE repository tonight with no human in
the loop, so the tests you write are the last thing between your change and a
fund that trades in the morning.

HOW TO WORK
- Write ONLY the files the spec lists. Anything else is mechanically rejected
  and the whole proposal is discarded — you cannot add a file later.
- Read the current contents carefully before changing them. Match the
  surrounding style: comment density, naming, idiom, error handling. Code that
  reads like the file it lives in is the standard here.
- Keep the diff minimal and aimed at the spec. Do not refactor adjacent code,
  add abstractions, or handle scenarios that cannot occur.
- Handle the failure modes the spec names, and fail SOFT on anything touching
  the live loop: a fault in new code must never stop the fund marking,
  rebalancing, or enforcing risk.
- Write tests that would FAIL before your change and pass after. A test that
  passes either way is worse than none, because it manufactures confidence.
  Cover the edge cases the spec names and the failure path you added.

INVARIANTS you may never weaken: S1 stays LLM-free (the control group); the
LLM never sends orders directly (the risk wrapper is mandatory); all books
share one ex-ante vol normalization; pre-registered decision rules are never
relaxed; degraded data must never trade; state schema changes are additive;
costs stay pessimistic; validation is out-of-sample only; the research armor
(pre-registration, lockbox, family Bonferroni) stays mechanical. If the spec
as written would violate one, implement the closest compliant version and say
exactly what you changed and why in test_note.

In test_note: state how a reviewer verifies this works, and what live number
should move if it is correct."""

_REVIEW_SYSTEM = """You are the adversarial reviewer and FINAL GATE of a
systematic fund's self-modifying platform. A diff you approve is applied to
the LIVE repository tonight with no human in the loop. You wrote the plan;
now attack the implementation.

You wrote the spec; now try to break the implementation. The engineer could
not see the rest of the repository — you can. So the questions only you can
ask are the ones that matter most: does this change do the right thing IN THIS
SYSTEM, not merely in its own files?

Attack it along these lines before deciding:
  - Completeness: is every caller, config key, and consumer of the changed
    behavior updated? A locally-correct change with an unupdated caller is the
    exact shape of the partial fixes this lane keeps having to redo.
  - Test honesty: would these tests have FAILED before the change? A test that
    passes either way manufactures confidence and is worse than none.
  - Blast radius: does this hand the platform a new power — writing, calling a
    service, spending, touching the host? If so, enumerate every context it
    can now fire in, INCLUDING the offline test suite and the sandbox, and
    reject unless those are closed. This is not hypothetical: it is how the
    suite came to restart the live Gateway.
  - Live safety: can a fault in this code stop the fund marking, rebalancing,
    or enforcing risk? Anything in that path must fail soft.
  - Invariants and one-way doors: S1 LLM-free, the risk wrapper, shared vol
    normalization, pre-registered rules, degraded-data no-trade, additive
    state, pessimistic costs, OOS-only validation, mechanical armor — and the
    lockbox shot, the pre-registration clock, executed orders, the ledgers.

Verdicts —
"approve": you would ship this to the live fund tonight, unattended. It does
what the spec asked, sweeps its own family where the spec said to, the tests
genuinely pin the new behavior, and nothing above is violated.
"discard": wrong, unsafe, or not worth its risk. Say why; it is rejected.
"revise": close, but you would not ship it as-is. Give precise, actionable
revision_instructions; a human decides in the morning.

A red sandbox is informative, not automatically fatal: if the failures are
clearly environmental, say so — but never approve when they touch the change
itself. When in doubt between approve and revise, choose revise: an
unnecessary morning review costs an hour, a bad unattended apply costs a day."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "memo": {"type": "string"},
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "kind": {"type": "string",
                             "enum": ["bugfix", "feature", "primitive",
                                      "ops", "test", "docs"]},
                    "risk_class": {"type": "string",
                                   "enum": ["low", "medium", "critical"]},
                    "rationale": {"type": "string"},
                    "test_note": {"type": "string"},
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"path": {"type": "string"},
                                           "content": {"type": "string"}},
                            "required": ["path", "content"],
                            "additionalProperties": False}},
                },
                "required": ["title", "kind", "risk_class", "rationale",
                             "test_note", "files"],
                "additionalProperties": False}},
    },
    "required": ["memo", "proposals"],
    "additionalProperties": False,
}

# E34 pipeline stage models. Division of labor by strength AND price: Fable-5
# ($10/$50 per MTok) does the judgment — planning and the adversarial gate,
# both small outputs; Claude Opus 5 ($5/$25, Anthropic's flagship coder) emits
# the bulk file contents at half Fable's token price. Net: better code AND a
# cheaper generated token than the single-Fable path.
PLAN_MODEL = "claude-fable-5"
GEN_MODEL = "claude-opus-5"
REVIEW_MODEL = "claude-fable-5"
FALLBACK_MODEL = "claude-opus-4-8"
# E41 — budget opened by the owner ("her gun bir sorunla ugrasmak istemiyorum"):
# buy DEPTH, not more features. `max` on the two judgment stages (plan sees the
# whole system; review is the last gate before an unattended live apply) and
# `xhigh` on the engineers, which is the documented setting for coding/agentic
# work and the one Claude Code itself defaults to. Engineers run concurrently,
# so effort costs latency per spec, not per night.
PLAN_EFFORT = "max"
GEN_EFFORT = "xhigh"
REVIEW_EFFORT = "max"

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "memo": {"type": "string"},
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "kind": {"type": "string",
                             "enum": ["bugfix", "feature", "primitive",
                                      "ops", "test", "docs"]},
                    "risk_class": {"type": "string",
                                   "enum": ["low", "medium", "critical"]},
                    "rationale": {"type": "string"},
                    "files_to_touch": {"type": "array",
                                       "items": {"type": "string"}},
                    "instructions": {"type": "string"},
                    "acceptance": {"type": "string"},
                },
                "required": ["title", "kind", "risk_class", "rationale",
                             "files_to_touch", "instructions", "acceptance"],
                "additionalProperties": False}},
    },
    "required": ["memo", "proposals"],
    "additionalProperties": False,
}

_GEN_SCHEMA = {
    "type": "object",
    "properties": {
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"path": {"type": "string"},
                               "content": {"type": "string"}},
                "required": ["path", "content"],
                "additionalProperties": False}},
        "test_note": {"type": "string"},
    },
    "required": ["files", "test_note"],
    "additionalProperties": False,
}

_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise", "discard"]},
        "reasons": {"type": "string"},
        "revision_instructions": {"type": ["string", "null"]},
    },
    "required": ["verdict", "reasons", "revision_instructions"],
    "additionalProperties": False,
}


def _stream_json(client, *, model: str, system: str, user: str, schema: dict,
                 max_tokens: int, effort: str | None = None,
                 fallback: str = FALLBACK_MODEL):
    """One structured-output call -> (data|None, resp|None, err|None).

    Server-side fallback covers the refusal classifiers on Fable-5 and Opus 5
    (array form + its matching beta header). Streaming because the generate
    stage runs at max_tokens=96000. All three pipeline stages and the legacy
    path go through here, so tests can script the whole pipeline by patching
    this one seam.
    """
    output_config: dict = {"format": {"type": "json_schema", "schema": schema}}
    if effort:
        output_config["effort"] = effort
    try:
        with client.beta.messages.stream(
            model=model,
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-06-01"],
            fallbacks=[{"model": fallback}],
            system=system,
            output_config=output_config,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            resp = stream.get_final_message()
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"
    if resp.stop_reason == "refusal":
        return None, resp, "refusal (whole fallback chain declined)"
    raw = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(raw), resp, None
    except json.JSONDecodeError as exc:
        trunc = resp.stop_reason == "max_tokens"
        return None, resp, (f"malformed JSON "
                            f"({'truncated at max_tokens' if trunc else exc}); "
                            f"{len(raw)} chars")


_GEN_USER_BUDGET = 250_000


def _gen_user(spec: dict) -> tuple[str | None, str | None]:
    """The implementing engineer's whole world: the spec + the COMPLETE current
    contents of exactly the files it may write. Returns (user_text, error).

    E42 — this function used to hand over `f.read_text()[:_MAX_FILE_CHARS]`
    with no marker, and the engineer emits COMPLETE replacement files. So for
    any file over 60,000 chars the engineer was asked to rewrite something it
    had only partly seen, and its emission would delete or hallucinate the
    tail. Both of the platform's most-edited files were over the cap when the
    lane found this (runtime/live.py 63,935; research/engineer.py 65,284) —
    the tail of the first holds the mirror gating and the run loop, the tail of
    the second holds _run_pipeline itself. A red sandbox would probably have
    caught it, but "probably" is not a guarantee: a tail of comments or
    untested code would pass green, which is silent code deletion.

    Truncation is therefore never acceptable here. A file that does not fit is
    a REJECTED SPEC with a loud reason — a wasted night beats a corrupted file.
    """
    parts = [f"=== CHANGE SPEC: {spec.get('title')} ===",
             f"kind: {spec.get('kind')} · risk: {spec.get('risk_class')}",
             f"RATIONALE:\n{spec.get('rationale')}",
             f"INSTRUCTIONS:\n{spec.get('instructions')}",
             f"ACCEPTANCE (the reviewer will check the diff for this):\n"
             f"{spec.get('acceptance')}"]
    used = sum(len(p) for p in parts)
    for p in spec.get("files_to_touch") or []:
        f = ROOT / p
        if f.exists():
            try:
                text = f.read_text()
            except (OSError, UnicodeDecodeError) as exc:
                return None, f"{p}: unreadable ({type(exc).__name__})"
        else:
            text = "[NEW FILE — does not exist yet]"
        blob = f"\n--- CURRENT FILE: {p} ---\n{text}"
        used += len(blob)
        if used > _GEN_USER_BUDGET:
            return None, (f"spec too large to implement safely: {p} pushes the "
                          f"engineer's context to {used:,} chars (budget "
                          f"{_GEN_USER_BUDGET:,}). Split the spec — a truncated "
                          f"read would make the engineer delete the file's tail.")
        parts.append(blob)
    parts.append("Emit the complete replacement files now.")
    return "\n\n".join(parts), None


def _review_user(rec: dict, spec: dict) -> str:
    pdir = PROPOSALS_DIR / rec["dir"]
    try:
        diff = (pdir / "diff.patch").read_text()[:60_000]
    except OSError:
        diff = "[diff unavailable]"
    sbx = rec.get("sandbox") or {}
    return "\n\n".join([
        f"=== PROPOSAL {rec['id']}: {rec['title']} ===",
        f"kind: {rec['kind']} · risk: {rec['risk_class']}",
        f"RATIONALE:\n{rec.get('rationale')}",
        f"YOUR ACCEPTANCE CRITERIA:\n{spec.get('acceptance', '—')}",
        f"ENGINEER'S TEST NOTE:\n{rec.get('test_note')}",
        f"SANDBOX: passed={sbx.get('passed')} · {str(sbx.get('note', ''))[:2000]}",
        f"=== DIFF ===\n{diff}",
        "Give your verdict.",
    ])


def _applied_tag(rec: dict) -> str:
    """Human-readable outcome of the auto-apply step for the nightly memo."""
    if rec.get("status") == "rejected":
        reasons = (rec.get("review") or {}).get("reasons") or rec.get("reject_reason")
        return f" DISCARDED by reviewer ({str(reasons)[:80]})"
    aa = rec.get("auto_apply")
    if not aa:
        return " (left pending)"
    if not aa.get("ok"):
        return f" APPLY-FAILED/rolled-back ({aa.get('error', '?')})"
    sha = (aa.get("git") or {}).get("sha")
    return f" APPLIED{' @' + sha if sha else ''}" + (
        " [needs restart]" if aa.get("needs_restart") else "")


def run_engineer(dry_run: bool = False) -> dict | None:
    """One nightly engineering session. Returns a digest dict or None."""
    from research.director import _budget_ok, _budget_spend

    cfg = _autonomy()
    if not cfg.get("engineer_enabled"):
        return None
    if cfg.get("mode") != "paper_flexible":
        return {"skipped": f"mode={cfg.get('mode')} — engineer lane locked"}
    max_pending = int(cfg.get("max_pending_proposals", 3))
    retired = retire_stale_proposals()   # E53: a corpse must not hold the gate
    if retired:
        print(f"🧹 retired {len(retired)} dead proposal(s): "
              + "; ".join(f"{r['id']} ({r['why']})" for r in retired))
    if pending_count() >= max_pending:
        return {"skipped": f"{pending_count()} proposals pending review "
                           f"(backpressure cap {max_pending})"}
    key = get_key("ANTHROPIC_API_KEY")
    # E44: this was hardcoded to 1, so "open the budget" (E41) raised effort,
    # parallelism and specs-per-night while a hidden one-session-per-day cap
    # kept the lane running at the old cadence — and on 2026-07-30 that single
    # session was consumed by a supervised test run, so the 04:30 timer skipped
    # the lane entirely. Sessions are the actual throughput lever; make it
    # visible and configurable.
    sessions = int(cfg.get("engineer_sessions_per_day", 1))
    if not key:
        return None
    # E48: budget exhaustion used to return None too, which is the same value
    # as "no key" — the caller could not tell "nothing to do" from "out of
    # room", and the settle loop needs that difference to know why it stopped.
    if not _budget_ok("engineer", sessions):
        return {"skipped": f"daily session budget spent ({sessions}/day)"}

    ctx = build_context()
    if dry_run:
        return {"dry_run": True, "context_chars": len(ctx)}

    max_p = int(cfg.get("max_proposals_per_night", 2))
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key, timeout=1200)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    # budget burns on the ATTEMPT, not on success — a failing session must not
    # retry-storm the API on every nightly re-invocation
    _budget_spend("engineer")
    if cfg.get("engineer_pipeline", True):
        return _run_pipeline(client, ctx, cfg, max_p)
    return _run_single(client, ctx, cfg, max_p)


def baseline_is_green(timeout: int = 900) -> dict:
    """Is the suite green under GATE conditions, before any money is spent?

    E58. The apply gate runs the full suite in a sandbox copy and rolls back on
    red. So if the suite is ALREADY red in that copy, every proposal the lane
    produces tonight is dead before it is written - and the lane has no way of
    knowing, because the suite is green on the host it runs on.

    That happened on 2026-08-02 and it cost roughly $25-30. A test I had
    written the day before read the live data/state.json, which the sandbox
    copy excludes. Green locally, green on the server, red in every sandbox.
    Six proposals generated at full Fable-plan and Opus-generate price, all six
    rolled back, none of them for anything to do with their own quality.

    The check is the cheap half of the expensive operation: one pytest run in a
    sandbox copy, no API calls, no tokens. Roughly a minute against roughly $11
    a session. Running it first turns "spend the night, discover the waste
    tomorrow" into "spend nothing, say why".

    Deliberately NOT dependent on anyone remembering to run a script. The
    parity script exists for humans; this is the machine checking itself.
    """
    infra = _timeout_infra_note() if "_timeout_infra_note" in globals() else ""
    tmp_root_dir = _sandbox_tmp_root()
    with tempfile.TemporaryDirectory(prefix="baseline_sbx_",
                                     dir=tmp_root_dir) as tmp:
        tmp_root = Path(tmp) / "repo"
        _copy_repo(tmp_root)
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pytest", "tests/", "-q",
                 "-p", "no:cacheprovider",
                 f"--basetemp={Path(tmp) / 'pytest-tmp'}", *_timeout_args()],
                cwd=tmp_root, capture_output=True, text=True, timeout=timeout)
        except Exception as exc:
            # An unrunnable baseline is not a licence to spend. If we cannot
            # establish that the gate would accept anything, we do not buy
            # anything.
            return {"green": False,
                    "summary": f"baseline could not run: {type(exc).__name__}: {exc}"}
        out = (r.stdout + r.stderr).strip().splitlines()
        failed = [l.split()[1] for l in out
                  if l.startswith("FAILED ") and len(l.split()) > 1]
        return {"green": r.returncode == 0,
                "failed": failed,
                "summary": "\n".join(out[-6:])}


def run_engineer_until_settled(dry_run: bool = False) -> dict | None:
    """Keep working through the night until there is nothing left worth doing.

    E48. The lane used to run exactly one session and withdraw, so a night's
    output was capped at whatever the planner happened to think of in its first
    look at the tree — even when the first round's own commits opened the
    obvious next piece of work. Sessions were already configurable (E44); what
    was missing was a reason to run a second one.

    Each round rebuilds context from disk, so round N+1 sees round N's applied
    commits and can build on them. That is the whole point: the lane iterates
    on a moving tree rather than planning once against a frozen snapshot.

    It stops for exactly one of these, and always says which:

    - `stood_down`   the planner proposed nothing. This is the lane's own
                     "everything looks fine, let us wait and see", and it is a
                     legitimate and expected way for a night to end.
    - `no_progress`  proposals were made but none survived review or the apply
                     gate, for `engineer_barren_rounds` rounds running. A clean
                     resubmit after a rejection is a real pattern, so one
                     barren round is not enough to quit.
    - `round_cap`    the configured ceiling on rounds.
    - `budget`       daily session budget spent.
    - `blocked`      backpressure, locked mode, or an error worth surfacing.

    A stop reason is never silence. Every round is reported even when the round
    did nothing, because "the lane ran and chose not to act" and "the lane
    never ran" are different facts and last week cost four days to that
    confusion.
    """
    cfg = _autonomy()
    max_rounds = int(cfg.get("engineer_max_rounds",
                             cfg.get("engineer_sessions_per_day", 1)))
    barren_cap = int(cfg.get("engineer_barren_rounds", 2))

    # E58: prove the gate would accept ANYTHING before buying a single token.
    base = baseline_is_green()
    if not base["green"]:
        named = ", ".join(base.get("failed") or []) or "see summary"
        return {"rounds": [], "n_rounds": 0, "applied_total": 0,
                "stop_reason": "baseline_red",
                "baseline": base,
                "detail": (f"suite is RED under gate conditions ({named}); "
                           f"every proposal tonight would be rolled back, so "
                           f"nothing was spent")}

    rounds: list[dict] = []
    applied_total = 0
    barren = 0
    stop = "round_cap"

    for _ in range(max(max_rounds, 1)):
        out = run_engineer(dry_run=dry_run)
        if out is None:                      # no key: not a failure, not a run
            return None
        rounds.append(out)
        if dry_run:
            stop = "dry_run"
            break
        if out.get("error"):
            stop = "blocked"
            break
        if out.get("skipped"):
            stop = "budget" if "budget" in out["skipped"] else "blocked"
            break
        if out.get("retryable"):
            barren += 1
            if barren >= barren_cap:
                stop = "no_progress"
                break
            continue

        accepted = out.get("accepted") or []
        rejected = out.get("rejected") or []
        applied = [r for r in accepted
                   if (r.get("auto_apply") or {}).get("ok")]
        applied_total += len(applied)

        if not accepted and not rejected:
            stop = "stood_down"
            break
        if applied:
            barren = 0
        else:
            barren += 1
            if barren >= barren_cap:
                stop = "no_progress"
                break

    return {"rounds": rounds, "stop_reason": stop,
            "applied_total": applied_total, "n_rounds": len(rounds)}


def _auto_apply(rec: dict, cfg: dict) -> dict:
    """Apply a green proposal, commit it, return the refreshed record (E30)."""
    res = apply_proposal(rec["id"], force_armor=True, run_tests=True)
    rec = next((p for p in list_proposals() if p["id"] == rec["id"]), rec)
    rec["auto_apply"] = res
    if res.get("ok") and cfg.get("auto_commit"):
        rec["auto_apply"]["git"] = git_commit(
            res["touched"] + [f"research/proposals/{rec['dir']}"],
            f"{rec['id']}: {rec['title']}\n\n"
            f"[{rec['kind']}/{rec['risk_class']}] auto-applied by the "
            f"engineer lane; full pytest green.\n\n"
            f"Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>")
    save_record(rec)
    return rec


def _write_memo(model: str, memo: str, accepted: list, rejected: list,
                extra: list[str] | None = None) -> None:
    with open(ENGINEER_MEMOS, "a") as fh:
        fh.write(f"\n## {datetime.now(timezone.utc).date().isoformat()} "
                 f"(model={model})\n{memo}\n"
                 + "".join(f"- {r['id']}: {r['title']} [{r['kind']}/"
                           f"{r['risk_class']}] sandbox="
                           f"{'✅' if r['sandbox']['passed'] else '❌'}"
                           f"{_applied_tag(r)}\n"
                           for r in accepted)
                 + "".join(f"- rejected at intake: {r['title']} ({r['error']})\n"
                           for r in rejected)
                 + "".join(f"{line}\n" for line in (extra or [])))


def _run_single(client, ctx: str, cfg: dict, max_p: int) -> dict:
    """Legacy single-call path (pre-E34): Fable plans AND writes the code in
    one emission. Kept behind engineer_pipeline: false as the rollback."""
    out, resp, err = _stream_json(
        client, model=PLAN_MODEL, system=_SYSTEM.format(max_p=max_p),
        user=ctx + "\n\nWrite tonight's engineering memo and your proposal(s).",
        schema=_SCHEMA, max_tokens=96000)
    if err:
        return ({"error": err} if resp is None
                else {"skipped": f"{err} — no proposal this run"})

    accepted, rejected = [], []
    for spec in out.get("proposals", [])[:max_p]:
        rec, werr = write_proposal(spec)
        if werr:
            rejected.append({"title": spec.get("title"), "error": werr})
            continue
        rec["sandbox"] = sandbox_test(PROPOSALS_DIR / rec["dir"])
        save_record(rec)
        # sandbox-green only — a red sandbox stays pending for a human
        if cfg.get("auto_apply") and rec["sandbox"].get("passed"):
            rec = _auto_apply(rec, cfg)
        accepted.append(rec)

    memo = out.get("memo", "")
    _write_memo(resp.model, memo, accepted, rejected)
    return {"memo": memo, "accepted": accepted, "rejected": rejected,
            "model": resp.model}


def _run_pipeline(client, ctx: str, cfg: dict, max_p: int) -> dict:
    """E34: plan (Fable) -> implement (Opus 5, xhigh) -> review (Fable) -> apply.

    The review is a real gate, not decoration: only an explicit "approve" from
    the reviewer auto-applies. "discard" rejects the proposal tonight;
    "revise" (or a review error, or a red sandbox) leaves it pending for the
    human. Two mechanical guarantees on the generator: it can only write files
    the plan named, and everything it writes still passes the same path
    validation + ast-parse + sandbox as before.
    """
    plan, resp, err = _stream_json(
        client, model=PLAN_MODEL, system=_PLAN_SYSTEM.format(max_p=max_p),
        user=ctx + f"\n\nWrite tonight's memo and 0-{max_p} change specs.",
        # E55: was 48000, the SMALLEST budget of the three big stages, handed
        # to the one that thinks hardest. On Fable 5 thinking is always on and
        # shares max_tokens with the response, and at effort=max it is deep.
        # On 2026-08-02 the planner spent essentially the whole 48000 on
        # thinking and the JSON came back truncated at 1057 chars, so the
        # entire autonomous night produced nothing. The generate stage already
        # runs at 96000 while doing less thinking than this one.
        schema=_PLAN_SCHEMA, max_tokens=96000, effort=PLAN_EFFORT)
    if err:
        if resp is None:
            return {"error": f"plan: {err}"}
        # E55: a truncated or malformed plan is a bad draw, not a verdict on the
        # night. It used to set `skipped`, which the settle loop reads as a
        # blocking condition and stops on - so one unlucky planning call ended a
        # run that had ten rounds of budget left. Marked retryable so the loop
        # counts it as a barren round and tries again with a fresh context.
        return {"retryable": f"plan: {err} — no proposal this round",
                "accepted": [], "rejected": []}

    memo = plan.get("memo", "")
    accepted, rejected, review_lines = [], [], []

    specs, claimed = [], set()
    for spec in (plan.get("proposals") or [])[:max_p]:
        touch = spec.get("files_to_touch") or []
        bad = next((f"{p}: {validate_rel_path(p)}"
                    for p in touch if validate_rel_path(p)), None)
        # concurrent engineers cannot see each other, so two specs sharing a
        # file would race: the second's "current contents" are already stale
        # and its emission would silently clobber the first.
        clash = sorted(set(touch) & claimed)
        if not touch or bad or clash:
            rejected.append({"title": spec.get("title"),
                             "error": bad or ("empty files_to_touch" if not touch
                                              else f"file claimed by an earlier spec: {clash}")})
            continue
        claimed.update(touch)
        specs.append(spec)

    # Generation is network-bound, so run the engineers CONCURRENTLY — wall
    # clock becomes the slowest spec rather than their sum. Sandbox and apply
    # stay strictly serial below: they are full pytest runs on a 2-vCPU box,
    # and they mutate the same working tree.
    # build every engineer's brief BEFORE dispatching: a spec whose files
    # cannot be handed over complete is rejected here rather than implemented
    # from a truncated read (E42)
    briefs: dict[int, str] = {}
    for i, spec in enumerate(specs):
        user, berr = _gen_user(spec)
        if berr:
            rejected.append({"title": spec.get("title"), "error": berr})
        else:
            briefs[i] = user
    specs = [s for i, s in enumerate(specs) if i in briefs]
    briefs = {i: briefs[k] for i, k in enumerate(sorted(briefs))}

    gens: dict[int, tuple] = {}
    if specs:
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=min(len(specs), 4)) as pool:
            futures = {pool.submit(
                _stream_json, client, model=GEN_MODEL, system=_GEN_SYSTEM,
                user=briefs[i], schema=_GEN_SCHEMA,
                max_tokens=96000, effort=GEN_EFFORT): i
                for i, s in enumerate(specs)}
            for fut in _cf.as_completed(futures):
                i = futures[fut]
                try:
                    gens[i] = fut.result()
                except Exception as exc:
                    gens[i] = (None, None, f"{type(exc).__name__}: {exc}")

    for i, spec in enumerate(specs):
        touch = spec.get("files_to_touch") or []
        gen, _gresp, gerr = gens.get(i, (None, None, "no result"))
        if gerr:
            rejected.append({"title": spec.get("title"),
                             "error": f"generate: {gerr}"})
            continue
        outside = [f["path"] for f in gen.get("files") or []
                   if f.get("path") not in set(touch)]
        if outside:
            rejected.append({"title": spec.get("title"),
                             "error": f"engineer wrote outside spec: {outside[:3]}"})
            continue

        rec, werr = write_proposal(
            {**{k: spec.get(k) for k in ("title", "kind", "risk_class",
                                         "rationale")},
             "test_note": gen.get("test_note"),
             "files": gen.get("files") or []})
        if werr:
            rejected.append({"title": spec.get("title"), "error": werr})
            continue
        rec["sandbox"] = sandbox_test(PROPOSALS_DIR / rec["dir"])
        save_record(rec)

        rev, _rresp, rerr = _stream_json(
            client, model=REVIEW_MODEL, system=_REVIEW_SYSTEM,
            user=_review_user(rec, spec), schema=_REVIEW_SCHEMA,
            max_tokens=16000, effort=REVIEW_EFFORT)
        verdict = (rev or {}).get("verdict") if not rerr else None
        rec["review"] = {"verdict": verdict,
                         "reasons": (rev or {}).get("reasons"),
                         "revision_instructions":
                             (rev or {}).get("revision_instructions"),
                         "error": rerr}
        save_record(rec)
        review_lines.append(
            f"  review[{rec['id']}]: {verdict or rerr}"
            f" — {str((rev or {}).get('reasons'))[:220]}")

        if (verdict == "approve" and rec["sandbox"].get("passed")
                and cfg.get("auto_apply")):
            rec = _auto_apply(rec, cfg)
        elif verdict == "discard":
            reject_proposal(rec["id"],
                            f"reviewer: {str((rev or {}).get('reasons'))[:300]}")
            rec = next((p for p in list_proposals() if p["id"] == rec["id"]), rec)
        # "revise" / review error / red sandbox -> pending for the human
        accepted.append(rec)

    _write_memo(resp.model, memo, accepted, rejected, extra=review_lines)
    return {"memo": memo, "accepted": accepted, "rejected": rejected,
            "model": resp.model}
