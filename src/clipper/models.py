"""Domain models shared across sources, ranking and storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from clipper.config import Platform


class ClipStatus(str, Enum):
    DISCOVERED = "discovered"
    RENDERING = "rendering"
    READY = "ready"          # rendered, waiting for human review
    FAILED = "failed"
    SKIPPED = "skipped"      # duplicate of a better clip of the same moment
    APPROVED = "approved"
    REJECTED = "rejected"
    POSTED = "posted"


@dataclass(frozen=True, slots=True)
class Clip:
    """A clip as reported by a source platform."""

    id: str
    platform: Platform
    broadcaster_login: str
    broadcaster_name: str
    title: str
    url: str
    view_count: int
    created_at: datetime
    duration_s: float
    vod_id: str | None = None
    vod_offset_s: int | None = None
    game_id: str | None = None

    def age_minutes(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return max((now - self.created_at).total_seconds() / 60.0, 0.0)

    @property
    def key(self) -> str:
        return f"{self.platform.value}:{self.id}"
