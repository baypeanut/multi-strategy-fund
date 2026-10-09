"""Entry point: live paper runtime + attribution dashboard (single process).

Runs the four-book loop and serves the dashboard on :8080. This is the file the
systemd `paper-trader` service runs on Hetzner.

  python scripts/run_paper_live.py --once          # configured feeds + paper tick
  python scripts/run_paper_live.py --interval 3600 # loop hourly (default)
"""
import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard.server import start_dashboard
from runtime.live import LiveRuntime


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run a single tick and exit")
    ap.add_argument("--interval", type=float, default=3600.0, help="seconds between ticks")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1", help="dashboard bind address; remote binding requires environment authentication and TLS/tunnel")
    ap.add_argument("--no-dashboard", action="store_true")
    ap.add_argument("--state", default="data/state.json")
    args = ap.parse_args()

    if not math.isfinite(args.interval) or args.interval <= 0:
        ap.error("--interval must be finite and positive")
    if not 1 <= args.port <= 65535:
        ap.error("--port must be between 1 and 65535")

    rt = LiveRuntime(state_path=args.state)
    server = None
    if not args.no_dashboard:
        server = start_dashboard(args.state, port=args.port, host=args.host)

    try:
        if args.once:
            out = rt.tick()
            print(out)
        else:
            rt.run(interval=args.interval)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
