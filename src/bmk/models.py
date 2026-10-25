"""Shared data types passed between ingesters, the service layer and adapters."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Protocol


@dataclass
class RawItem:
    """One URL as emitted by an ingester, before validation or dedupe."""

    url: str
    title: str | None = None
    event_at: datetime | None = None  # time stated by the source
    event_at_kind: str | None = None  # 'exact' | 'user' | 'file_birthtime' | 'file_mtime'
    source_ref: str | None = None  # device name / file path / message-id
    locator: str | None = None  # line no., sheet!row, tab uuid
    extra: dict[str, str] = field(default_factory=dict)  # leftover fields → remarks on new bookmarks
    observed_kind: str | None = None  # CloudTabs: 'first_seen' | 'first_seen_initial'
    observed_by: datetime | None = None  # source says the item already existed by then (CloudTabs last_viewed_time)


class Ingester(Protocol):
    source_type: str

    def read(self) -> Iterable[RawItem]: ...


OUTCOMES = (
    "new",
    "matched_exact",
    "matched_alias",
    "unchanged",
    "invalid",
    "skipped",
    "suppressed_deleted",
    "suppressed_resaved",
)


@dataclass
class ImportSummary:
    run_id: int
    source_type: str
    counts: dict[str, int] = field(default_factory=lambda: {o: 0 for o in OUTCOMES})
    error: str | None = None

    @property
    def seen(self) -> int:
        return sum(self.counts.values())

    def line(self) -> str:
        parts = [f"{k}={v}" for k, v in self.counts.items() if v]
        return f"run {self.run_id} [{self.source_type}] seen={self.seen} " + " ".join(parts)


EDITABLE_FIELDS = ("title", "url", "remarks", "saved_at", "status", "starred", "category_id")
# Edited by the user but never locked: nothing automatic changes triage, and a category records who
# assigned it instead (category_assigned_by 'user' keeps AI proposals away from it).
TRIAGE_FIELDS = ("status", "starred", "category_id")
# Triage: everything new lands in the Inbox until it is kept (or moved to Trash, which is deleted_at).
STATUSES = ("inbox", "kept")
