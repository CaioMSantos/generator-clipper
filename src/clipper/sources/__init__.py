"""Clip sources (one module per platform)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from clipper.config import Streamer
from clipper.models import Clip


class SourceError(Exception):
    """Raised when a platform API call fails in a non-recoverable way."""


class ClipSource(Protocol):
    def fetch_clips(self, streamers: list[Streamer], since: datetime) -> list[Clip]: ...
