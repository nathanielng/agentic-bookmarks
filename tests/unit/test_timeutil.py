from bmk.timeutil import better_saved_at, display, to_iso
from datetime import datetime, timedelta, timezone

EARLY = "2026-10-01T00:00:00Z"
LATE = "2026-10-20T00:00:00Z"


def test_anything_beats_unknown():
    assert better_saved_at(None, "unknown", LATE, "file_mtime")


def test_unknown_never_replaces():
    assert not better_saved_at(LATE, "file_mtime", None, "unknown")


def test_better_precision_wins_even_if_later():
    assert better_saved_at(EARLY, "file_mtime", LATE, "exact")


def test_worse_precision_loses_even_if_earlier():
    assert not better_saved_at(LATE, "exact", EARLY, "first_seen")


def test_equal_precision_earlier_wins():
    assert better_saved_at(LATE, "exact", EARLY, "exact")
    assert not better_saved_at(EARLY, "exact", LATE, "exact")


def test_observation_kinds_share_rank():
    # A later first_seen sighting must not replace an earlier first_seen_initial upper bound.
    assert not better_saved_at(EARLY, "first_seen_initial", LATE, "first_seen")
    assert better_saved_at(LATE, "first_seen_initial", EARLY, "first_seen")


def test_to_iso_normalises_to_utc():
    sgt = timezone(timedelta(hours=8))
    assert to_iso(datetime(2026, 10, 25, 13, 30, tzinfo=sgt)) == "2026-10-25T05:30:00Z"


def test_display_markers():
    assert display(None, "unknown") == "—"
    assert display("2026-10-25T05:30:00Z", "exact") == "2026-10-25 13:30"
    assert display("2026-10-25T05:30:00Z", "first_seen_initial") == "~2026-10-25 13:30"
