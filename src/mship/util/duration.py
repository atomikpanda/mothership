from datetime import datetime, timezone


def format_relative(dt: datetime, *, _now: datetime | None = None) -> str:
    """Return a short 'N ago' string for a datetime relative to now.

    Naive datetimes are interpreted as UTC. Future datetimes and zero-second
    deltas render as 'just now'. Deltas over 30 days render as '30+ days ago'.
    """
    now = _now if _now is not None else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    delta = now - dt
    total = int(delta.total_seconds())
    if total <= 0:
        return "just now"
    if total < 60:
        return f"{total}s ago"

    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m ago"

    hours, rem_min = divmod(minutes, 60)
    if hours < 24:
        if rem_min:
            return f"{hours}h {rem_min}m ago"
        return f"{hours}h ago"

    days, rem_hours = divmod(hours, 24)
    if days > 30:
        return "30+ days ago"
    if rem_hours:
        return f"{days}d {rem_hours}h ago"
    return f"{days}d ago"
