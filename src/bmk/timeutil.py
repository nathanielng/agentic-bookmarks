"""UTC timestamp helpers and the saved_at precedence policy (PLAN05 §4.1)."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

# Best precision first. 'unknown' always loses.
SAVED_AT_PRECEDENCE = [
    "exact",
    "user",
    "first_seen",
    "first_seen_initial",
    "file_birthtime",
    "file_mtime",
    "unknown",
]
# Both observation kinds are upper bounds on the real save time, so they share a rank and the
# earliest observation wins between them.
_RANK = {"exact": 0, "user": 1, "first_seen": 2, "first_seen_initial": 2, "file_birthtime": 3, "file_mtime": 4, "unknown": 5}

APPROXIMATE_SOURCES = {"first_seen_initial", "file_birthtime", "file_mtime"}


def now_utc() -> str:
    return to_iso(datetime.now(UTC))


def to_iso(dt: datetime) -> str:
    """Normalise to UTC ISO-8601 with second precision. Naive datetimes are assumed UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def rank(source: str) -> int:
    return _RANK.get(source, _RANK["unknown"])


def better_saved_at(current_at: str | None, current_src: str, new_at: str | None, new_src: str) -> bool:
    """True if (new_at, new_src) should replace the current saved_at.

    Better precision wins; with equal precision, the earlier time wins.
    """
    if new_at is None or new_src == "unknown":
        return False
    if current_at is None or current_src == "unknown":
        return True
    if rank(new_src) != rank(current_src):
        return rank(new_src) < rank(current_src)
    return parse_iso(new_at) < parse_iso(current_at)


def display(value: str | None, source: str, tz: str = "Asia/Singapore", short: bool = False) -> str:
    """Human display: '—' if unknown, '~' prefix if approximate. `short` drops the year."""
    if not value:
        return "—"
    local = parse_iso(value).astimezone(ZoneInfo(tz)).strftime("%m-%d %H:%M" if short else "%Y-%m-%d %H:%M")
    return f"~{local}" if source in APPROXIMATE_SOURCES else local
