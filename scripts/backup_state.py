"""Off-box backup of the fund's irreplaceable ledgers (E19 deferred item).

state.json is the fund's ONLY accounting record; research/registry.json and
RESULTS.jsonl are the research armor's append-only ledgers. They all live on
one box. This script snapshots them into a timestamped tar.gz under
data/backups/, prunes old snapshots, and (optionally) pushes the archive
off-box via a user-configured shell command (rclone / rsync / scp — anything
that accepts the archive path). Secrets are excluded BY CONSTRUCTION: a file
named .env can never enter an archive, so off-box copies cannot leak keys.

It also maintains data/equity_daily.jsonl: one append-only JSON line per
COMPLETED UTC date per series (each book's equity curve, the benchmark, the
common-universe diagnostic indices), lifted out of state.json's ring buffers
before the runtime's 3000-mark cap (~125 days at 24 marks/day) can evict the
oldest days for good. Same defect class as E36 (fills) and P0009 (sentiment):
an irreplaceable series behind a display-sized ring. Today is still accruing
marks and is never written, existing lines are never rewritten, and a failure
here can never break the backup itself.

Config (all optional, config.yaml):
  backup:
    retention: 14                                    # local snapshots to keep
    remote_cmd: "rclone copy {archive} remote:fund-backups"

Run manually, or via cron on the server:
  17 3 * * * cd /opt/fund && venv/bin/python scripts/backup_state.py >> logs/backup.log 2>&1
"""
from __future__ import annotations

import json
import math
import shlex
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.alerts import send_telegram
from core.config import CONFIG

ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = ROOT / "data" / "backups"
EQUITY_LEDGER = ROOT / "data" / "equity_daily.jsonl"

# The files that cannot be regenerated if the box dies. .env is deliberately
# NOT here and is refused even if someone adds it (see make_archive).
LEDGERS = [
    "data/state.json",
    "data/sentiment_history.jsonl",   # scored-headline history (H1 stage 1)
    "data/fills_history.jsonl",       # realized IBKR fills (E23 cost calibration)
    "data/s3_decisions.jsonl",        # S3 decision history (S3-v2 dataset)
    "data/equity_daily.jsonl",        # daily closes lifted out of state.json's
                                      # 3000-mark ring before it can evict them
    "research/registry.json",
    "research/RESULTS.jsonl",
    "research/hypotheses.yaml",
    "research/NIGHTLY_MEMOS.md",
    "research/ENGINEER_MEMOS.md",
    "RESEARCH_LOG.md",
    "config/config.yaml",
    "data/universe/equities.json",
]


def update_equity_ledger(state_path: Path = ROOT / "data" / "state.json",
                         ledger_path: Path = EQUITY_LEDGER,
                         now: datetime | None = None) -> int:
    """Append each COMPLETED day's closing value per series to the ledger.

    Reads state.json (read-only) and extracts, per series and per UTC date,
    the LAST mark of that date — the close. Series are the books of
    state['equity_history'] (name kept as-is), state['benchmark'] as 'bench',
    and each key k of state['common_idx_history'] as 'common_<k>'.

    The ledger is append-only (mode 'a' only, exactly like fills_history.jsonl):
    already-written (date, series) pairs are skipped, corrupt lines are left
    where they are, and today's date is never written because it is still
    accruing marks. Missing/unreadable/malformed state -> 0 rows, and no path
    can raise: a broken ledger update must never break the nightly backup.
    Returns the number of rows appended.
    """
    try:
        stamp = now if now is not None else datetime.now(timezone.utc)
        today = stamp.date().isoformat()

        try:
            state = json.loads(state_path.read_text())
        except Exception:
            return 0                      # no state / unreadable / bad JSON
        if not isinstance(state, dict):
            return 0                      # wrong shape

        # {series: {date: value}} — later entries of the same date overwrite
        # earlier ones, so what survives is that date's closing value.
        closes: dict[str, dict[str, float]] = {}

        def _absorb(series: str, entries) -> None:
            if not isinstance(entries, list):
                return
            per_day = closes.setdefault(series, {})
            for e in entries:
                if not isinstance(e, (list, tuple)) or len(e) < 2:
                    continue              # one bad row never kills the rest
                ts, val = e[0], e[1]
                if not isinstance(ts, str) or len(ts) < 10:
                    continue
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    continue
                if not math.isfinite(val):
                    continue              # NaN/inf are not valid JSON
                per_day[ts[:10]] = val

        books = state.get("equity_history", {}) or {}
        if isinstance(books, dict):
            for book, entries in books.items():
                if isinstance(book, str):
                    _absorb(book, entries)
        _absorb("bench", state.get("benchmark") or [])
        idx = state.get("common_idx_history", {}) or {}
        if isinstance(idx, dict):
            for k, entries in idx.items():
                if isinstance(k, str):
                    _absorb(f"common_{k}", entries)

        # What the ledger already holds. Blank/corrupt/non-dict lines are
        # tolerated and left untouched — the file is never rewritten.
        seen: set[tuple[str, str]] = set()
        if ledger_path.exists():
            try:
                text = ledger_path.read_text()
            except Exception:
                text = ""
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue
                d, s = row.get("date"), row.get("series")
                if isinstance(d, str) and isinstance(s, str):
                    seen.add((d, s))

        rows = []
        for series, per_day in closes.items():
            for d, val in per_day.items():
                if d >= today:
                    continue              # today is still accruing marks
                if (d, series) in seen:
                    continue
                rows.append((d, series, val))
        if not rows:
            return 0
        rows.sort(key=lambda r: (r[0], r[1]))     # deterministic output

        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("a") as fh:
            for d, series, val in rows:
                fh.write(json.dumps({"date": d, "series": series,
                                     "value": val}) + "\n")
        return len(rows)
    except Exception:
        return 0


def make_archive(root: Path = ROOT, out_dir: Path = BACKUP_DIR,
                 ledgers: list[str] | None = None) -> Path:
    """Snapshot the ledgers into a timestamped tar.gz.

    Missing files are skipped silently (a fresh box has no RESULTS.jsonl
    yet); anything named .env is refused even if explicitly listed — an
    off-box archive must never become a secrets leak.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = out_dir / f"fund_ledgers_{ts}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for rel in (ledgers if ledgers is not None else LEDGERS):
            if Path(rel).name == ".env":
                continue                      # secrets never enter an archive
            p = root / rel
            if p.exists():
                tar.add(p, arcname=rel)
    return archive


def prune(out_dir: Path = BACKUP_DIR, retention: int | None = None) -> int:
    """Delete all but the newest `retention` snapshots. Returns count removed."""
    cfg = CONFIG.get("backup", {}) or {}
    keep = retention if retention is not None else int(cfg.get("retention", 14))
    snaps = sorted(out_dir.glob("fund_ledgers_*.tar.gz"))
    victims = snaps[:-keep] if keep > 0 else []
    for v in victims:
        v.unlink()
    return len(victims)


def push_offbox(archive: Path, remote_cmd: str | None = None) -> bool:
    """Run the configured off-box push command with {archive} substituted.

    Not configured -> no-op (local-only backup). Failure alerts via Telegram
    but never raises — a broken remote must not break the local snapshot.
    """
    cfg = CONFIG.get("backup", {}) or {}
    cmd = remote_cmd if remote_cmd is not None else cfg.get("remote_cmd")
    if not cmd:
        return False
    try:
        r = subprocess.run(shlex.split(cmd.format(archive=str(archive))),
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[:200])
        return True
    except Exception as exc:
        send_telegram(f"⚠️ BACKUP off-box push FAILED: {type(exc).__name__}: {exc}")
        return False


def main() -> None:
    n = update_equity_ledger()        # capture closes BEFORE the archive is cut
    archive = make_archive()
    removed = prune()
    pushed = push_offbox(archive)
    size_kb = archive.stat().st_size / 1024
    print(f"backup written {archive} ({size_kb:.0f} KB), equity ledger +{n} rows, "
          f"pruned {removed}, "
          f"off-box={'ok' if pushed else 'not configured / failed'}")


if __name__ == "__main__":
    main()
