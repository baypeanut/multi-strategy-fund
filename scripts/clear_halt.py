"""Human-in-the-loop halt clear.

The governor's halt LATCHES: once tripped, all books stay flat until a human
runs this script. The live process keeps state in memory and would otherwise
overwrite a naive disk edit on its next _save - so this script clears
state.json AND the running runtime reconciles via _sync_halt_clear_from_disk
at the start of every tick (no restart required).

Usage (on the server): python scripts/clear_halt.py
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.alerts import send_telegram

STATE = Path(__file__).resolve().parent.parent / "data" / "state.json"


def main() -> None:
    if not STATE.exists():
        print(f"no state file at {STATE}")
        return
    state = json.loads(STATE.read_text())
    latch = state.pop("halt_latched", None)
    if not latch:
        print("no halt latched - nothing to clear")
        return
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE)
    print(f"cleared halt from {latch['ts']} (reason: {latch['reason']})")
    print("live runtime will lift the latch on its next tick (no restart needed)")
    send_telegram(f"✅ halt cleared by human (was: {latch['reason']})",
                  urgent=True)


if __name__ == "__main__":
    main()
