"""Timestamps that read the same from either backend.

Postgres hands back `timestamptz` as an aware `datetime`; SQLite hands back the
ISO string that was stored. Comparing those two directly raises, so every
Python-side deadline check goes through `as_datetime` first. SQL-side comparisons
need no help: Postgres casts the bound parameter, and SQLite compares ISO strings,
which sort chronologically.
"""

from __future__ import annotations

from datetime import UTC, datetime


def now() -> str:
    """The single spelling of 'now' that gets written to the database."""
    return datetime.now(UTC).isoformat()


def as_datetime(value: str | datetime | None) -> datetime | None:
    """Coerce a stored timestamp from either backend to an aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def is_past(value: str | datetime | None, *, at: datetime | None = None) -> bool:
    """True when the stored deadline has already passed."""
    moment = as_datetime(value)
    if moment is None:
        return False
    return moment <= (at or datetime.now(UTC))
