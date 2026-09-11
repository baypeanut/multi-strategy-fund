"""Telegram alerting — fire-and-forget, never raises.

Uses TELEGRAM_* credentials in .env; prefixes every message with [FUND].
Alerting must never take the trading loop down: every failure path swallows
and returns False.

Default ops posture: one daily digest. Only `urgent=True` messages hit the
wire immediately (halt latch / human clear / backup failure). Everything else
is either queued into state for the digest or dropped.
"""
from __future__ import annotations

import json
import ssl
import urllib.request
from datetime import datetime
from typing import Any

import certifi

from .env import get_key

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
PREFIX = "[FUND] "


def send_telegram(message: str, timeout: int = 10, *, urgent: bool = True) -> bool:
    """Send a Telegram message.

    `urgent=True` (default) hits the API now. Callers that want the daily-only
    posture must pass `urgent=False`, which is a no-op at the wire — use
    `queue_note` + `maybe_send_daily_digest` instead.
    """
    if not urgent:
        return False
    token = get_key("TELEGRAM_BOT_TOKEN")
    chat_id = get_key("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": chat_id, "text": PREFIX + message}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            return resp.status == 200
    except Exception:
        return False


def queue_note(state: dict[str, Any], text: str, *, limit: int = 40) -> None:
    """Buffer a one-liner for the next daily digest (deduped, capped)."""
    notes: list[str] = list(state.setdefault("telegram_notes", []))
    text = (text or "").strip()
    if not text or (notes and notes[-1] == text):
        return
    notes.append(text)
    state["telegram_notes"] = notes[-limit:]


def format_daily_digest(state: dict[str, Any], now: datetime) -> str:
    """Compact once-a-day fund status for Telegram."""
    nav0 = float(state.get("nav0") or 0.0) or 1.0
    lines = [f"Daily summary · {now.date().isoformat()} UTC"]
    systems = state.get("systems") or {}
    # every book the fund is actually marking — S5 (the forward shadow of the
    # confirmed 8k-drift edge) was invisible here for its entire life because
    # this tuple was frozen at four; a future book appears without an edit
    for key in sorted(systems) if systems else ("s1", "s2", "s3", "s4"):
        sysd = systems.get(key) or {}
        eq = float(sysd.get("equity") or nav0)
        ret = eq / nav0 - 1.0
        lines.append(f"{key.upper()} ${eq:,.0f} ({ret:+.2%} vs start)")

    halt = state.get("halt_latched")
    if halt:
        lines.append(f"HALT latched since {halt.get('ts', '?')}: {halt.get('reason', '')}")
    else:
        lines.append("Halt: none")

    dm = state.get("s3_vs_s1") or {}
    if dm.get("n") is not None:
        p = dm.get("p_value")
        p_s = "—" if p is None else f"{p:.3f}"
        lines.append(
            f"S3−S1 DM: n={dm.get('n')} p={p_s} "
            f"statistical_gate={'yes' if dm.get('verdict_allowed') else 'no'}"
        )
        ratio = dm.get("vol_ratio_s3_s1")
        if ratio is not None:
            lines.append(f"S3/S1 realized vol: {ratio:.2f}x")
        if dm.get("risk_comparable") is False:
            lines.append("RISK MISMATCH: no superiority claim")
        elif dm.get("verdict_allowed") and dm.get("risk_comparable") is not True:
            lines.append("Risk comparability unknown: no superiority claim")

    ibk = state.get("ibkr") or {}
    if ibk.get("nav"):
        book = ibk.get("book") or {}
        lines.append(
            f"IBKR {ibk.get('account', '')}: NAV ${ibk['nav']:,.0f} · "
            f"{ibk.get('n_positions', '?')} pos · "
            f"max|drift|={book.get('max_abs_drift', '—')}"
        )

    actions = state.get("last_actions") or []
    if actions:
        lines.append("Governor: " + "; ".join(actions[:3]))

    notes = state.get("telegram_notes") or []
    if notes:
        lines.append("Notes:")
        lines.extend(f"· {n}" for n in notes[-12:])

    ticks = state.get("ticks")
    last = state.get("last_tick")
    if ticks is not None:
        lines.append(f"Ticks={ticks} · last={last}")
    return "\n".join(lines)


def maybe_send_daily_digest(
    state: dict[str, Any],
    now: datetime,
    *,
    hour_utc: int = 21,
) -> bool:
    """Send at most one digest per UTC day, after `hour_utc`.

    Returns True if a message was sent.
    """
    if now.hour < int(hour_utc):
        return False
    today = now.date().isoformat()
    if state.get("telegram_digest_date") == today:
        return False
    msg = format_daily_digest(state, now)
    ok = send_telegram(msg[:3800], urgent=True)
    if ok:
        state["telegram_digest_date"] = today
        state["telegram_notes"] = []
    return ok
