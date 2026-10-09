"""Local interface demo using generated fixtures, with no market or broker APIs.

The fixture exists only in a private temporary directory. Its prices, equity
curves and fill rows illustrate the interface and are not strategy results.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import tempfile
import threading

from dashboard.server import start_dashboard


def make_demo_state(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    nav0 = 100_000.0
    dates = []
    day = now - timedelta(days=30)
    while day < now:
        if day.weekday() < 5:
            dates.append(day.replace(hour=20, minute=0, second=0, microsecond=0).isoformat())
        day += timedelta(days=1)
    histories = {
        book: [
            [date, round(nav0 + 180 * math.sin(i / 3 + offset), 2)] for i, date in enumerate(dates)
        ]
        for offset, book in enumerate(("s1", "s2", "s3", "s4"))
    }
    timestamp = now.isoformat()
    return {
        "demo_metadata": {
            "synthetic": True,
            "description": "Generated interface fixtures; no market data, strategy performance or executed orders.",
        },
        "nav0": nav0,
        "last_tick": timestamp,
        "ticks": 0,
        "data_provider": "synthetic-fixture",
        "systems": {
            book: {"equity": history[-1][1], "weights": {"DEMO_A": 0.02, "DEMO_B": -0.01}}
            for book, history in histories.items()
        },
        "equity_history": histories,
        "s3_pm_source": "held",
        "s3_pm_counts": {},
        "s4_attribution": [["DEMO_A", 0.02, 0.001, 2.0], ["DEMO_B", -0.01, 0.002, -2.0]],
        "last_actions": ["Synthetic fixture — no trades executed"],
        "data_incidents": [],
        "llm_budget": {"pm": 0, "pm_cap": 0, "scorer": 0, "scorer_cap": 0},
        "ibkr": {
            "account": "DEMO_ONLY",
            "mode": "synthetic-fixture",
            "nav": nav0,
            "last_refresh": timestamp,
            "n_positions": 1,
            "positions": [
                {
                    "symbol": "DEMO_A",
                    "shares": 20,
                    "price": 100,
                    "mv": 2000,
                    "weight": 0.02,
                    "target_w": 0.02,
                }
            ],
            "fills": [
                {
                    "side": "SYNTHETIC",
                    "shares": 20,
                    "symbol": "DEMO_A",
                    "price": 100,
                    "time": timestamp,
                }
            ],
        },
    }


def run_demo(
    port: int = 8080, *, duration: float | None = None, stop_event: threading.Event | None = None
) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be an integer from 0 to 65535")
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise ValueError("duration must be finite and positive")
    with tempfile.TemporaryDirectory(prefix="fund-ui-demo-") as directory:
        state_path = Path(directory) / "synthetic-state.json"
        fd = os.open(state_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(make_demo_state(), stream)
        server = start_dashboard(str(state_path), host="127.0.0.1", port=port, demo_mode=True)
        print("DEMO: synthetic interface fixtures only. No API calls, model jobs or broker orders.")
        try:
            (stop_event or threading.Event()).wait(timeout=duration)
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", type=int, default=8080, help="Loopback port; use 0 for an available port"
    )
    parser.add_argument(
        "--duration", type=float, help="Optional number of seconds before automatic shutdown"
    )
    args = parser.parse_args()
    try:
        run_demo(port=args.port, duration=args.duration)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
