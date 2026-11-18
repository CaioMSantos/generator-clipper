from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import respx

from clipper.config import Platform, Streamer
from clipper.sources.kick import CLIPS_URL, KickSource, matches_game

SINCE = datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc)
URL = CLIPS_URL.format(slug="davyjones")


def _raw(clip_id: str, created: str, views: int = 10, category: str = "Grand Theft Auto VI (GTA)") -> dict[str, Any]:
    # Shape copied from a real kick.com/api/v2 response (2026-11-18).
    return {
        "id": clip_id, "livestream_id": "138509909", "category_id": "13155", "title": " fuga ",
        "views": views, "view_count": views, "duration": 32, "started_at": "0001-01-01T00:00:00Z",
        "created_at": created, "vod_starts_at": 0,
        "category": {"id": "13155", "name": category, "slug": "grand-theft-auto-vi"},
        "channel": {"id": "1", "username": "DavyJones", "slug": "davyjones"},
    }  # fmt: skip


def _streamers() -> list[Streamer]:
    return [Streamer(name="Davy", platform=Platform.KICK, login="DavyJones")]


def test_matches_game_accepts_kick_suffix() -> None:
    assert matches_game("Grand Theft Auto VI (GTA)", ["Grand Theft Auto VI"])
    assert matches_game("grand theft auto vi", ["Grand Theft Auto VI"])
    assert not matches_game("Grand Theft Auto V (GTA)", ["Grand Theft Auto VI"])
    assert not matches_game("IRL", ["Grand Theft Auto VI"])


@respx.mock
def test_paginates_by_views_drops_old_and_maps_fields() -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json={
                "clips": [_raw("clip_A", "2026-11-18T07:34:07.812294Z", views=55)],
                "nextCursor": '{"view":0,"id":"clip_A"}',
            }),
            httpx.Response(200, json={
                "clips": [_raw("clip_B", "2026-11-18T01:00:00Z"), _raw("clip_OLD", "2026-11-17T23:00:00Z")],
                "nextCursor": None,
            }),
        ]
    )  # fmt: skip
    clips = KickSource().fetch_clips(_streamers(), SINCE)
    assert [c.id for c in clips] == ["clip_A", "clip_B"]
    assert route.call_count == 2  # stopped when nextCursor is null
    assert route.calls[0].request.url.params["sort"] == "view"
    assert route.calls[1].request.url.params["cursor"] == '{"view":0,"id":"clip_A"}'
    a = clips[0]
    assert a.url == "https://kick.com/davyjones/clips/clip_A"
    assert (a.view_count, a.title, a.broadcaster_name, a.vod_offset_s) == (55, "fuga", "DavyJones", None)
    assert a.created_at == datetime(2026, 11, 18, 7, 34, 7, 812294, tzinfo=timezone.utc)


@respx.mock
def test_game_filter_drops_other_categories() -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(200, json={
            "clips": [_raw("gta", "2026-11-18T05:00:00Z"), _raw("irl", "2026-11-18T04:00:00Z", category="IRL")],
            "nextCursor": None,
        })
    )  # fmt: skip
    clips = KickSource(game_names=["Grand Theft Auto VI"]).fetch_clips(_streamers(), SINCE)
    assert [c.id for c in clips] == ["gta"]


@respx.mock
def test_cloudflare_403_returns_empty_without_raising() -> None:
    respx.get(URL).mock(return_value=httpx.Response(403, text="<html>cf</html>"))
    assert KickSource().fetch_clips(_streamers(), SINCE) == []
