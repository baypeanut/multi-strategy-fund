"""Human-in-the-loop halt clear.

The governor's halt LATCHES: once tripped, all books stay flat until a human
runs this script. The live process keeps state in memory and rewrites
state.json every tick, so the clear has to reach the running runtime rather
than just the file.

This writes a SENTINEL and nothing else. It used to read state.json, drop the
latch key, and write the whole file back — a read-modify-write on the fund's
only state file, performed by a second process while the first one was live.
Nothing serialised the two. A tick landing inside that window would have its
save clobbered by the stale copy this script had already read, and the marks,
prices and equity rows that tick had just written would be gone. Low
probability, and a hole in the evidence is not a thing to accept at any
probability when the alternative costs one file.

The runtime clears the latch itself, in its own save cycle, and deletes the
sentinel. One writer, no window.

Usage (on the server): python scripts/clear_halt.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.alerts import send_telegram

DATA = Path(__file__).resolve().parent.parent / "data"
STATE = DATA / "state.json"
SENTINEL = DATA / "halt_clear.request"


def main() -> None:
    if not STATE.exists():
        print(f"no state file at {STATE}")
        return
    try:
        latch = json.loads(STATE.read_text()).get("halt_latched")
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read state ({exc}); refusing to guess")
        return
    if not latch:
        print("no halt latched - nothing to clear")
        return

    SENTINEL.parent.mkdir(parents=True, exist_ok=True)
    SENTINEL.write_text(json.dumps({"requested_at": latch.get("ts"),
                                    "reason": latch.get("reason")}, indent=2))
    print(f"clear requested for halt from {latch.get('ts')} "
          f"(reason: {latch.get('reason')})")
    print("the live runtime lifts the latch on its next tick (no restart needed)")
    send_telegram(f"halt clear requested by human (was: {latch.get('reason')})",
                  urgent=True)


if __name__ == "__main__":
    main()
