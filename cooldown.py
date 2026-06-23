"""
cooldown.py — Prevent re-entering a ticker too soon after an exit.

Without this rule, the bot can enter a market, exit on STALE_THESIS at
break-even, then re-enter the same market the next signal run. Each round
trip pays the bid-ask spread and the thesis hasn't actually changed.

Cooldown logic:
  - Applied per Kalshi ticker, across both bots' journals
  - Triggered by exit reasons: STALE_THESIS, VOLUME_EXIT, TARGET_HIT, cancelled
  - NOT triggered by SETTLED_WIN / SETTLED_LOSS (the market resolved — we
    couldn't re-enter even if we wanted to)
  - Default window: 24 hours
"""

from datetime import datetime, timedelta, timezone

# Exit reasons that should block re-entry for the cooldown window
COOLDOWN_TRIGGERS = {
    "STALE_THESIS",
    "VOLUME_EXIT",
    "TARGET_HIT",
    "STOP_LOSS",
}

DEFAULT_COOLDOWN_HOURS = 24


def is_in_cooldown(ticker: str, trades: list[dict], hours: int = DEFAULT_COOLDOWN_HOURS) -> bool:
    """
    Returns True if this ticker had an early exit (or cancellation) within
    the cooldown window.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    for t in trades:
        if t.get("kalshi_ticker") != ticker:
            continue
        status = t.get("status")
        if status not in ("settled", "exited", "cancelled"):
            continue

        # Skip if market actually resolved — re-entry was impossible anyway,
        # and a NEW market for the same event would have a different ticker.
        reason = t.get("exit_reason") or ""
        is_cancelled = status == "cancelled"
        if not is_cancelled and reason not in COOLDOWN_TRIGGERS:
            continue  # e.g. SETTLED_WIN — don't block

        exit_time_str = t.get("settled_at") or t.get("cancelled_at")
        if not exit_time_str:
            continue
        try:
            exit_time = datetime.fromisoformat(exit_time_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue

        if exit_time > cutoff:
            return True

    return False


def cooldown_remaining_hours(ticker: str, trades: list[dict], hours: int = DEFAULT_COOLDOWN_HOURS) -> float:
    """For diagnostic logging — how much cooldown is left on this ticker (0 if none)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    latest_exit = None
    for t in trades:
        if t.get("kalshi_ticker") != ticker:
            continue
        status = t.get("status")
        if status not in ("settled", "exited", "cancelled"):
            continue
        reason = t.get("exit_reason") or ""
        is_cancelled = status == "cancelled"
        if not is_cancelled and reason not in COOLDOWN_TRIGGERS:
            continue
        exit_time_str = t.get("settled_at") or t.get("cancelled_at")
        if not exit_time_str:
            continue
        try:
            exit_time = datetime.fromisoformat(exit_time_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if latest_exit is None or exit_time > latest_exit:
            latest_exit = exit_time

    if latest_exit is None or latest_exit < cutoff:
        return 0.0
    return (latest_exit + timedelta(hours=hours) - datetime.now(timezone.utc)).total_seconds() / 3600
