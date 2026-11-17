from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from clipper.config import Platform
from clipper.models import Clip

NOW = datetime(2026, 11, 19, 22, 0, tzinfo=timezone.utc)


def make_clip(
    id: str = "c1",
    login: str = "alanzoka",
    views: int = 100,
    age_min: float = 30,
    vod_id: str | None = "v1",
    vod_offset: int | None = 1000,
    platform: Platform = Platform.TWITCH,
) -> Clip:
    return Clip(
        id=id,
        platform=platform,
        broadcaster_login=login,
        broadcaster_name=login.title(),
        title=f"title {id}",
        url=f"https://clips.twitch.tv/{id}",
        view_count=views,
        created_at=NOW - timedelta(minutes=age_min),
        duration_s=30,
        vod_id=vod_id,
        vod_offset_s=vod_offset,
    )


@pytest.fixture
def now() -> datetime:
    return NOW
