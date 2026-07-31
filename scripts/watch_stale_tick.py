"""Stale-tick pager (E27 follow-up) - a wedged loop must page within ~2h.

E27's incident: the tick loop froze ~8 HOURS on a blocked broker socket while
systemd showed the service `active` - a live-but-wedged process is invisible
to Restart= policies and the 26h fund-watchdog bound is an order of magnitude
too loose for an hourly loop. This script is the independent detector: run
from cron, it reads data/state.json (the runtime's heartbeat: `last_tick`)
and fires an URGENT Telegram when the tick is staler than the threshold.
Independence is the point - a wedged loop cannot be trusted to report its
own wedge, so the monitor lives outside the process entirely.

It NEVER writes state.json (the runtime owns that file); its own dedupe
ledger lives in data/watchdog.json, so a wedged loop pages at most once per
realert window instead of every cron pass, and recovery sends exactly one
all-clear. A missing/unreadable state.json also pages: a runtime that never
started (E25's root-owned-junk crash class) is the same emergency.

Second check (2026-07-25): IBKR-link staleness. The broker calls are bounded
and fail-safe by design (E27/E29), so a wedged Gateway degrades SILENTLY
observed live 2026-07-25: no successful broker read from 03:48 onward, every
2h mirror timing out at 120s and every hourly refresh at 45s, with the
Gateway's own ~23:45 daily restart the only recovery path. While the link is
down a governor halt-flatten cannot reach the paper account (the E29
risk-control gap) and the fill-calibration dataset stalls. This check pages
the human within ~ibkr_stale_hours so a manual `systemctl restart ib-gateway`
closes the gap instead of waiting hours. Skipped entirely when the mirror is
disabled in config; its dedupe keys are namespaced (ibkr_*) so the two
pagers never disturb each other's state.

Third check (2026-07-26): applied-but-not-running code. Under E30 the engineer
lane applies and commits its own work unattended, but it cannot restart a
service - so a fix touching runtime/core/dashboard sits DORMANT until a human
notices. Observed live: P0011 (the fills-ledger data-loss fix) landed 04:43 and
was still not running at 12:37 - 8h in which the very dataset it rescues went
on being destroyed, with the repo, the tests and git all looking perfectly
healthy. Nothing in the system could see that gap: `systemctl` reports active,
the tick is fresh, and the code on disk is correct. This check compares the
newest mtime under runtime/, core/ and dashboard/ against the service's start
time and pages when code is newer, naming the restart command.

Config (all optional, config.yaml):
  watchdog:
    stale_tick_minutes: 150     # ~2.5 missed hourly ticks -> page
    realert_hours: 6            # re-page cadence while still stale
    ibkr_stale_hours: 3         # ~3 missed hourly refreshes -> page
    restart_grace_minutes: 20   # ignore code newer than the process by < this

Cron (server):
  */30 * * * * cd /opt/fund && venv/bin/python scripts/watch_stale_tick.py >> logs/watchdog.log 2>&1
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.alerts import send_telegram
from core.config import CONFIG

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "state.json"
LEDGER = ROOT / "data" / "watchdog.json"


def _load_ledger(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_ledger(path: Path, led: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(led, indent=1))


def _tick_age_minutes(state_path: Path, now: datetime) -> float | None:
    """Minutes since the runtime's last heartbeat, or None if unreadable."""
    try:
        last_tick = json.loads(state_path.read_text()).get("last_tick")
    except (OSError, json.JSONDecodeError):
        return None
    if not last_tick:
        return None
    try:
        ts = datetime.fromisoformat(last_tick)
    except ValueError:
        return None
    if ts.tzinfo is None:                 # heartbeat wobble must not crash us
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 60.0


def check(state_path: Path = STATE, ledger_path: Path = LEDGER,
          now: datetime | None = None) -> dict:
    """One monitoring pass.

    Pages (urgent Telegram) when the heartbeat is staler than the threshold;
    dedupes re-pages via the ledger file; sends exactly one all-clear on
    recovery. Returns a small dict describing what happened (for the cron
    log and for tests).
    """
    now = now or datetime.now(timezone.utc)
    cfg = CONFIG.get("watchdog", {}) or {}
    stale_min = float(cfg.get("stale_tick_minutes", 150))
    realert_h = float(cfg.get("realert_hours", 6))
    led = _load_ledger(ledger_path)

    age_min = _tick_age_minutes(state_path, now)
    if age_min is None:
        stale = True
        detail = f"{state_path} missing/unreadable - runtime heartbeat absent"
    else:
        stale = age_min > stale_min
        detail = (f"last tick {age_min:.0f} min ago "
                  f"(threshold {stale_min:.0f} min)")

    if stale:
        due = True
        last_alert = led.get("last_alert")
        if last_alert:
            try:
                due = ((now - datetime.fromisoformat(last_alert))
                       .total_seconds() >= realert_h * 3600)
            except ValueError:
                due = True
        if due:
            send_telegram(
                "⏰ STALE TICK - " + detail
                + ". Loop may be wedged while systemd shows 'active' "
                  "(E27 class): check paper-trader / ib-gateway.")
            led["last_alert"] = now.isoformat()
            led["alerting"] = True
            _save_ledger(ledger_path, led)
        return {"stale": True, "alerted": due, "detail": detail}

    if led.get("alerting"):
        # one all-clear, then silence until the next incident
        send_telegram("✅ tick resumed - " + detail)
        led["alerting"] = False
        led.pop("last_alert", None)
        _save_ledger(ledger_path, led)
        return {"stale": False, "alerted": False, "recovered": True,
                "detail": detail}
    return {"stale": False, "alerted": False, "detail": detail}


# ---------------------------------------------------------- IBKR link -------
def _restart_unit(unit: str) -> dict:
    """Restart a systemd unit via the narrow sudoers rule (restart-only,
    exact units). Verifies the unit is active before claiming success."""
    import subprocess
    import time
    try:
        r = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "restart", unit],
                           capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            return {"ok": False,
                    "error": (r.stdout + r.stderr).strip()[:200] or "sudo denied"}
        time.sleep(10)
        chk = subprocess.run(["systemctl", "is-active", unit],
                             capture_output=True, text=True, timeout=15)
        state = chk.stdout.strip()
        return {"ok": state == "active",
                **({} if state == "active" else {"error": f"unit state: {state}"})}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _parse_ts(ts) -> datetime | None:
    """ISO timestamp -> aware UTC datetime; None on anything unparseable."""
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t


def check_ibkr(state_path: Path = STATE, ledger_path: Path = LEDGER,
               now: datetime | None = None) -> dict:
    """IBKR-link staleness pass.

    A healthy loop produces one successful broker read per hour (refresh, or
    sync on rebalance ticks), each stamping last_refresh/last_sync. When the
    newest stamp is older than ibkr_stale_hours the Gateway is presumed
    wedged: page, with the runtime's stamped refresh_error included so the
    human sees WHY without opening the dashboard. Dedupe/recovery mirror the
    tick pager but under namespaced ledger keys. An unreadable state.json is
    deliberately NOT this check's emergency - the tick pager owns that page.
    """
    now = now or datetime.now(timezone.utc)
    if not (CONFIG.get("ibkr", {}) or {}).get("enabled"):
        return {"stale": False, "alerted": False, "detail": "ibkr disabled"}
    cfg = CONFIG.get("watchdog", {}) or {}
    stale_h = float(cfg.get("ibkr_stale_hours", 3))
    realert_h = float(cfg.get("realert_hours", 6))

    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"stale": False, "alerted": False,
                "detail": "state unreadable (tick pager's emergency)"}
    ibk = state.get("ibkr") or {}
    if not ibk:
        # mirror enabled but never attempted yet (fresh deploy) - not a wedge
        return {"stale": False, "alerted": False,
                "detail": "no ibkr state yet"}

    stamps = [t for t in (_parse_ts(ibk.get("last_refresh")),
                          _parse_ts(ibk.get("last_sync"))) if t is not None]
    if stamps:
        age_h = (now - max(stamps)).total_seconds() / 3600.0
        stale = age_h > stale_h
        detail = (f"last successful IBKR read {age_h:.1f}h ago "
                  f"(threshold {stale_h:.0f}h)")
    else:
        stale = True
        detail = "IBKR mirror enabled but no successful read ever recorded"
    err = ibk.get("refresh_error")
    if err:
        detail += f" · last error: {str(err)[:80]}"

    led = _load_ledger(ledger_path)
    if stale:
        due = True
        last_alert = led.get("ibkr_last_alert")
        if last_alert:
            try:
                due = ((now - datetime.fromisoformat(last_alert))
                       .total_seconds() >= realert_h * 3600)
            except ValueError:
                due = True
        if due:
            # E34 self-healing: two distinct Gateway wedge classes in one week
            # (07-23 clientId hold, 07-25 full unresponsiveness) showed the
            # daily 23:45 restart is not enough. With auto_restart_gateway the
            # watchdog restarts the unit itself (once per realert window - the
            # same dedupe that bounds the pages bounds the restarts) and the
            # page reports what it did. Off by default in code; enabled in
            # config.yaml so the posture is explicit and revertible.
            note = ""
            if cfg.get("auto_restart_gateway", False):
                res = _restart_unit("ib-gateway")
                note = (" Auto-restarted ib-gateway - verifying next pass."
                        if res.get("ok") else
                        f" Auto-restart FAILED ({str(res.get('error'))[:80]})"
                        " - manual restart needed.")
            send_telegram(
                "🔌 IBKR LINK STALE - " + detail
                + ". Gateway likely wedged: a halt-flatten cannot reach the "
                  "paper account until it recovers (E29 gap)." + note)
            led["ibkr_last_alert"] = now.isoformat()
            led["ibkr_alerting"] = True
            _save_ledger(ledger_path, led)
        return {"stale": True, "alerted": due, "detail": detail}

    if led.get("ibkr_alerting"):
        send_telegram("✅ IBKR link recovered - " + detail)
        led["ibkr_alerting"] = False
        led.pop("ibkr_last_alert", None)
        _save_ledger(ledger_path, led)
        return {"stale": False, "alerted": False, "recovered": True,
                "detail": detail}
    return {"stale": False, "alerted": False, "detail": detail}


_CODE_DIRS = ("runtime", "core", "dashboard")
_SERVICE = "paper-trader"


def _service_started_at(service: str = _SERVICE) -> datetime | None:
    """When the running unit started, per systemd. None if unavailable (not a
    systemd host, or the unit is unknown) - the check then no-ops rather than
    guessing, since a false 'restart needed' page trains the human to ignore it."""
    import subprocess
    try:
        out = subprocess.run(
            ["systemctl", "show", service, "-p", "ActiveEnterTimestamp", "--value"],
            capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out or out == "n/a":
        return None
    for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %Z"):
        try:
            return datetime.strptime(out, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _newest_code_mtime(root: Path = ROOT) -> tuple[datetime | None, str]:
    newest, where = None, ""
    for d in _CODE_DIRS:
        base = root / d
        if not base.exists():
            continue
        for f in base.rglob("*.py"):
            if "__pycache__" in f.parts:
                continue
            try:
                m = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
            except OSError:
                continue
            if newest is None or m > newest:
                newest, where = m, str(f.relative_to(root))
    return newest, where


def check_restart(ledger_path: Path = LEDGER, now: datetime | None = None,
                  service: str = _SERVICE, root: Path = ROOT) -> dict:
    """Applied-but-not-running pass: is the code on disk newer than the process?

    The lane ships code unattended but cannot restart the service, so this is
    the only signal that an applied fix is not actually in effect. Dedupe /
    re-page / one all-clear mirror the other two pagers, under restart_* keys.
    """
    now = now or datetime.now(timezone.utc)
    cfg = CONFIG.get("watchdog", {}) or {}
    grace_m = float(cfg.get("restart_grace_minutes", 20))
    realert_h = float(cfg.get("realert_hours", 6))

    started = _service_started_at(service)
    if started is None:
        return {"stale": False, "alerted": False, "detail": "service start time unavailable"}
    newest, where = _newest_code_mtime(root)
    if newest is None:
        return {"stale": False, "alerted": False, "detail": "no code files found"}

    lag_m = (newest - started).total_seconds() / 60.0
    stale = lag_m > grace_m
    detail = (f"newest code {where} is {lag_m:.0f}min newer than the running "
              f"{service}" if stale else
              f"{service} is running code no older than {where}")

    led = _load_ledger(ledger_path)
    if stale:
        due = True
        last_alert = led.get("restart_last_alert")
        if last_alert:
            try:
                due = ((now - datetime.fromisoformat(last_alert))
                       .total_seconds() >= realert_h * 3600)
            except ValueError:
                due = True
        if due:
            send_telegram(
                "♻️ RESTART NEEDED - " + detail
                + ". An applied fix is NOT in effect until the service reloads: "
                  f"`systemctl restart {service}`.")
            led["restart_last_alert"] = now.isoformat()
            led["restart_alerting"] = True
            _save_ledger(ledger_path, led)
        return {"stale": True, "alerted": due, "detail": detail}

    if led.get("restart_alerting"):
        send_telegram("✅ Restart done - " + detail)
        led["restart_alerting"] = False
        led.pop("restart_last_alert", None)
        _save_ledger(ledger_path, led)
        return {"stale": False, "alerted": False, "recovered": True, "detail": detail}
    return {"stale": False, "alerted": False, "detail": detail}


def main() -> None:
    tick = check()
    ibkr = check_ibkr()
    restart = check_restart()
    print(f"{datetime.now(timezone.utc).isoformat()} tick={tick} ibkr={ibkr} "
          f"restart={restart}")


if __name__ == "__main__":
    main()
