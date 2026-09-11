"""Morning review CLI for Staff-Engineer proposals (E22).

  python scripts/review_proposals.py                       # list all
  python scripts/review_proposals.py --show P0003          # memo + diff
  python scripts/review_proposals.py --sandbox P0003       # re-run sandbox pytest
  python scripts/review_proposals.py --apply P0003         # backup -> write -> pytest -> rollback on fail
  python scripts/review_proposals.py --apply P0003 --force-armor
  python scripts/review_proposals.py --reject P0003 --reason "why"

Apply runs on THIS machine only — remember to sync the other copy (local vs
Hetzner) and restart paper-trader when runtime/dashboard/core changed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research.engineer import (PROPOSALS_DIR, apply_proposal, list_proposals,
                               reject_proposal, sandbox_test)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show")
    ap.add_argument("--sandbox")
    ap.add_argument("--apply", dest="apply_id")
    ap.add_argument("--force-armor", action="store_true")
    ap.add_argument("--no-tests", action="store_true")
    ap.add_argument("--reject")
    ap.add_argument("--reason", default="")
    args = ap.parse_args()

    if args.show:
        rec = next((p for p in list_proposals() if p["id"] == args.show), None)
        if not rec:
            sys.exit(f"{args.show} not found")
        pdir = PROPOSALS_DIR / rec["dir"]
        print((pdir / "MEMO.md").read_text())
        sbx = rec.get("sandbox") or {}
        print(f"sandbox: {'PASS' if sbx.get('passed') else 'FAIL'} — "
              f"{sbx.get('summary', 'not run')}\n")
        print((pdir / "diff.patch").read_text())
        return

    if args.sandbox:
        rec = next((p for p in list_proposals() if p["id"] == args.sandbox), None)
        if not rec:
            sys.exit(f"{args.sandbox} not found")
        print(sandbox_test(PROPOSALS_DIR / rec["dir"]))
        return

    if args.apply_id:
        out = apply_proposal(args.apply_id, force_armor=args.force_armor,
                             run_tests=not args.no_tests)
        print(out)
        sys.exit(0 if out.get("ok") else 1)

    if args.reject:
        if not args.reason:
            sys.exit("--reject requires --reason (it feeds the agent's learning)")
        print(reject_proposal(args.reject, args.reason))
        return

    rows = list_proposals()
    if not rows:
        print("no proposals yet")
        return
    for p in rows:
        sbx = p.get("sandbox") or {}
        print(f"{p['id']}  [{p.get('status'):8}]  {p.get('kind','?'):9} "
              f"{p.get('risk_class','?'):8}  sandbox="
              f"{'PASS' if sbx.get('passed') else 'FAIL/—'}  {p.get('title')}")


if __name__ == "__main__":
    main()
