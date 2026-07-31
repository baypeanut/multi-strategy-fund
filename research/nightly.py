"""Nightly research orchestration v2 (E17).

Order of operations each night:
  1. CONFIRMATION first: if any family has earned its one lockbox shot, fire
     it (this is the highest-value experiment the system can run).
  2. Otherwise run the queue head (register -> execute -> record).
  3. Referee: adversarial Opus memo on any PASS/CANDIDATE/CONFIRMED result.
  4. Director: one Fable design session queues tomorrow's experiments.
  5. Print digest (Telegram is the live runtime's once-daily fund summary).

Every step degrades gracefully: no key/budget/director failure -> the static
queue still runs exactly like the v1 agent. The agent never touches live
config; PASS/CONFIRMED are flags for HUMAN review.

Run: python research/nightly.py [--dry-run]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from research.director import run_director, run_referee
from research.harness import (HYPOTHESES, already_run_discovery, execute_spec,
                              load_registry, pending_confirmation, record,
                              register_spec, run_confirmation, validate_spec)


def load_queue() -> list[dict]:
    if HYPOTHESES.exists():
        return yaml.safe_load(HYPOTHESES.read_text()) or []
    return []


def save_queue(queue: list[dict]) -> None:
    HYPOTHESES.write_text(yaml.safe_dump(queue, sort_keys=False, allow_unicode=True))


def run_one_from_queue(queue: list[dict]) -> dict | None:
    for hypo in queue:
        if hypo.get("status", "pending") != "pending":
            continue
        # already executed under a prior (possibly reset) queue state? skip it
        # without re-running - re-running would re-inflate the family's trial
        # count and corrupt the multiple-testing correction. This is the
        # immutable guard; the mutable yaml status is only a convenience.
        prior = already_run_discovery(hypo)
        if prior:
            hypo["status"] = "done"
            hypo["result_id"] = prior
            hypo["skipped_duplicate"] = True
            save_queue(queue)
            continue
        err = validate_spec(hypo)
        if err:
            hypo["status"] = "rejected"
            hypo["reject_reason"] = err
            save_queue(queue)
            continue
        register_spec(hypo)
        metrics, error = None, None
        try:
            metrics = execute_spec(hypo, window="explore")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        entry = record(hypo, metrics, error, phase="discovery")
        hypo["status"] = "done"
        hypo["result_id"] = entry["id"]
        save_queue(queue)
        return entry
    return None


def main() -> None:
    dry = "--dry-run" in sys.argv
    queue = load_queue()
    digest: list[str] = []
    entries: list[dict] = []

    # 1) confirmation has absolute priority
    fam = pending_confirmation()
    if fam:
        entry = run_confirmation(fam)
        entries.append(entry)
        digest.append(
            f"🎯 LOCKBOX CONFIRMATION [{fam}]: {entry['verdict']}"
            + (f" | {json.dumps(entry['metrics'])}" if entry["metrics"]
               else f" | error: {entry['error']}"))
        if entry["verdict"] == "CONFIRMED":
            digest.append("⚠️ CONFIRMED - human review required before anything "
                          "goes near live capital")
    else:
        # 2) queue head
        entry = run_one_from_queue(queue)
        if entry:
            entries.append(entry)
            digest.append(
                f"🔬 {entry['id']} {entry['spec'].get('name')} "
                f"[{entry['family']}]: {entry['verdict']}"
                + (f" | {json.dumps(entry['metrics'])}" if entry["metrics"]
                   else f" | error: {entry['error']}"))
        else:
            digest.append("🔬 queue empty - director will design")

    # 3) adversarial referee on notable results
    for entry in entries:
        if entry["verdict"] in ("PASS", "CONFIRMED"):
            memo = run_referee(entry)
            if memo:
                digest.append(f"⚔️ referee: {memo[:400]}")

    # 4) director designs tomorrow
    out = run_director(load_queue())
    if out and out.get("error"):
        # E45: surface it. A silent director is indistinguishable from a
        # deliberate empty night, and that cost three days of dead research.
        digest.append(f"🧠 DIRECTOR FAILED: {out['error']}")
    elif out:
        queue = load_queue()
        queue.extend(out["accepted"])
        save_queue(queue)
        digest.append(f"🧠 director ({out['model']}): "
                      f"+{len(out['accepted'])} spec | memo: {out['memo'][:300]}")
        if out.get("rejected"):
            digest.append("🧠 director specs rejected: " + "; ".join(
                f"{r['name']}: {r['error']}" for r in out["rejected"][:3]))

    reg = load_registry()
    n_pending = len([h for h in load_queue()
                     if h.get("status", "pending") == "pending"])
    digest.append(f"global N={reg['total_experiments']} | queue: {n_pending} pending")
    msg = "\n".join(digest)
    print(msg)
    # Telegram: fund sends one daily digest from live.py; research stays in
    # logs + RESEARCH_LOG so the chat is not a second daily channel.
    if dry:
        print("(dry-run - no side effects beyond the printed digest)")


if __name__ == "__main__":
    main()
