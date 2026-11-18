from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from clipper.config import Platform, Settings, Streamer, StreamerCatalog
from clipper.media.transcribe import Word
from clipper.models import Clip, ClipStatus
from clipper.pipeline import Pipeline
from clipper.sources.twitch import HELIX_URL, TOKEN_URL, TwitchSource
from clipper.store import Store
from tests.conftest import NOW, make_clip


def _streamers(*logins: str) -> list[Streamer]:
    return [Streamer(name=lg, platform=Platform.TWITCH, login=lg) for lg in logins]


@respx.mock
def test_twitch_fetch_paginates_filters_game_and_refreshes_token() -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
    respx.get(f"{HELIX_URL}/users").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "42", "login": "alanzoka"}]})
    )
    respx.get(f"{HELIX_URL}/games").mock(return_value=httpx.Response(200, json={"data": [{"id": "999"}]}))
    clip_raw = {
        "id": "Clip1", "url": "https://clips.twitch.tv/Clip1", "broadcaster_name": "alanzoka",
        "title": "kkk", "view_count": 120, "created_at": "2026-11-19T21:30:00Z", "duration": 28.5,
        "video_id": "v1", "vod_offset": 900, "game_id": "999",
    }
    other_game = {**clip_raw, "id": "Clip2", "game_id": "1"}
    clips_route = respx.get(f"{HELIX_URL}/clips").mock(
        side_effect=[
            httpx.Response(401),  # expired token -> refresh and retry
            httpx.Response(200, json={"data": [clip_raw], "pagination": {"cursor": "abc"}}),
            httpx.Response(200, json={"data": [other_game], "pagination": {}}),
        ]
    )
    src = TwitchSource("id", "secret", game_names=["Grand Theft Auto VI"])
    clips = src.fetch_clips(_streamers("alanzoka", "ghost"), NOW - timedelta(hours=6))

    assert [c.id for c in clips] == ["Clip1"]
    assert clips[0].vod_offset_s == 900 and clips[0].view_count == 120
    assert clips_route.call_count == 3
    assert clips_route.calls[-1].request.url.params["after"] == "abc"


@respx.mock
def test_twitch_game_filter_disabled_when_game_not_found() -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
    respx.get(f"{HELIX_URL}/users").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "42", "login": "coringa"}]})
    )
    respx.get(f"{HELIX_URL}/games").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(f"{HELIX_URL}/clips").mock(
        return_value=httpx.Response(200, json={"data": [{
            "id": "X", "url": "u", "view_count": 1, "created_at": "2026-11-19T21:30:00Z", "game_id": "1",
        }]})
    )
    clips = TwitchSource("id", "s", game_names=["Nope"]).fetch_clips(_streamers("coringa"), NOW)
    assert len(clips) == 1


class FakeSource:
    def __init__(self, clips: list[Clip]) -> None:
        self.clips = clips

    def fetch_clips(self, streamers: list[Streamer], since: datetime) -> list[Clip]:
        return self.clips


class FakeTranscriber:
    def transcribe(self, media: Path) -> list[Word]:
        return []


@pytest.fixture
def pipeline(tmp_path: Path) -> Pipeline:
    settings = Settings(CLIPPER_DATA_DIR=tmp_path, CLIPPER_MIN_VIEWS=0)
    catalog = StreamerCatalog(streamers=_streamers("alanzoka"))
    return Pipeline(settings, catalog, Store(settings.db_path), [], FakeTranscriber())


def test_discover_queues_best_take_and_never_rerenders_moment(pipeline: Pipeline) -> None:
    a = make_clip("a", views=100, vod_offset=1000)
    b = make_clip("b", views=300, vod_offset=1010)
    pipeline.sources = [FakeSource([a, b])]

    pipeline.discover(NOW)
    st = pipeline.store.statuses([a.key, b.key])
    assert st == {a.key: ClipStatus.SKIPPED, b.key: ClipStatus.DISCOVERED}

    claimed = pipeline.store.claim_next(5, NOW - timedelta(hours=6))
    assert [r.clip.id for r in claimed] == ["b"]
    pipeline.store.set_status(b.key, ClipStatus.READY, output_path="/x.mp4")

    # Later, the other take overtakes in views: the moment is already handled.
    a2 = make_clip("a", views=900, vod_offset=1000)
    pipeline.sources = [FakeSource([a2, b])]
    result = pipeline.discover(NOW)
    assert result.queued == 0
    assert pipeline.store.statuses([a.key])[a.key] == ClipStatus.SKIPPED


def test_skipped_take_is_promoted_when_it_becomes_best_before_render(pipeline: Pipeline) -> None:
    a = make_clip("a", views=100, vod_offset=1000)
    b = make_clip("b", views=300, vod_offset=1010)
    pipeline.sources = [FakeSource([a, b])]
    pipeline.discover(NOW)
    pipeline.sources = [FakeSource([make_clip("a", views=900, vod_offset=1000), b])]
    pipeline.discover(NOW)
    st = pipeline.store.statuses([a.key, b.key])
    assert st == {a.key: ClipStatus.DISCOVERED, b.key: ClipStatus.SKIPPED}


def test_claim_ignores_stale_clips_and_recover_stuck(pipeline: Pipeline) -> None:
    old = make_clip("old", age_min=60 * 10)
    pipeline.store.upsert_discovered(old, 999, 1)
    assert pipeline.store.claim_next(5, NOW - timedelta(hours=6)) == []

    fresh = make_clip("fresh")
    pipeline.store.upsert_discovered(fresh, 1, 1)
    assert len(pipeline.store.claim_next(5, NOW - timedelta(hours=6))) == 1
    assert pipeline.store.recover_stuck() == 1


@respx.mock
def test_twitch_fetch_clip_by_id() -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
    route = respx.get(f"{HELIX_URL}/clips").mock(
        side_effect=[
            httpx.Response(200, json={"data": [{
                "id": "Abc", "url": "https://clips.twitch.tv/Abc", "broadcaster_name": "Alanzoka",
                "title": "que jogada", "view_count": 7, "created_at": "2026-11-19T21:30:00Z",
            }]}),
            httpx.Response(200, json={"data": []}),
        ]
    )  # fmt: skip
    src = TwitchSource("id", "s")
    clip = src.fetch_clip("Abc")
    assert clip is not None
    assert (clip.broadcaster_login, clip.title, clip.view_count) == ("alanzoka", "que jogada", 7)
    assert route.calls[0].request.url.params["id"] == "Abc"
    assert src.fetch_clip("Gone", "coringa") is None


def test_suggested_caption_has_no_leading_blank_lines_without_title() -> None:
    import dataclasses

    from clipper.pipeline import _metadata

    meta = _metadata(dataclasses.replace(make_clip(), title=""), 0.0, [])
    assert str(meta["suggested_caption"]).startswith("Créditos:")


def test_claim_is_round_robin_and_capped_per_streamer(tmp_path: Path) -> None:
    store = Store(tmp_path / "q.sqlite3")
    for i, score in enumerate([90.0, 80.0, 70.0, 60.0]):  # heavy streamer, all better scores
        store.upsert_discovered(make_clip(f"a{i}", login="alanzoka", vod_offset=i * 100), score, 1)
    store.upsert_discovered(make_clip("d0", login="davyjones", platform=Platform.KICK), 5.0, 1)
    store.upsert_discovered(make_clip("d1", login="davyjones", platform=Platform.KICK), 4.0, 1)

    claimed = store.claim_next(4, NOW - timedelta(hours=6), per_streamer=2)
    assert [r.clip.id for r in claimed] == ["a0", "d0", "a1", "d1"]
    # Without a cap the best scores win, as before.
    assert [r.clip.id for r in store.claim_next(4, NOW - timedelta(hours=6))] == ["a2", "a3"]


def test_clips_over_duration_limit_are_dropped(pipeline: Pipeline) -> None:
    import dataclasses

    short = make_clip("short", vod_offset=100)
    long_clip = dataclasses.replace(make_clip("long", vod_offset=5000), duration_s=180)
    unknown = dataclasses.replace(make_clip("unknown", vod_offset=9000), duration_s=0)
    pipeline.sources = [FakeSource([short, long_clip, unknown])]

    pipeline.discover(NOW)
    claimed = pipeline.store.claim_next(10, NOW - timedelta(hours=6))
    assert sorted(r.clip.id for r in claimed) == ["short", "unknown"]


def test_queued_long_clip_is_skipped_not_rendered(pipeline: Pipeline) -> None:
    import dataclasses

    long_clip = dataclasses.replace(make_clip("long"), duration_s=180)
    pipeline.store.upsert_discovered(long_clip, 10.0, 1)  # queued before the limit existed
    assert pipeline.process_queue(NOW) == 0
    assert pipeline.store.statuses([long_clip.key]) == {long_clip.key: ClipStatus.SKIPPED}
