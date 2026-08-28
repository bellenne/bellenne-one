from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return naive UTC for SQLite DateTime columns without legacy utcnow()."""
    return datetime.now(UTC).replace(tzinfo=None)
