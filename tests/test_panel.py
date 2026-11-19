from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clipper.config import Platform, Settings, Streamer, StreamerCatalog
from clipper.models import ClipStatus
from clipper.panel.app import create_app
from clipper.pipeline import Pipeline, RenderResult, shift_words
from clipper.media.transcribe import Word
from clipper.store import Store
from tests.conftest import make_clip


class FakePipeline:
    """Stands in for Pipeline in the re-render worker: records the call, writes a fake mp4."""

    def __init__(self, store: Store, out_dir: Path) -> None:
        self.store = store
        self.out_dir = out_dir
        self.calls: list[dict[str, Any]] = []

    def render_clip(self, clip: Any, **kwargs: Any) -> RenderResult:
        self.calls.append(kwargs)
        out = self.out_dir / f"{clip.id}.mp4"
        out.write_bytes(b"new")
        meta = out.with_suffix(".json")
        meta.write_text(json.dumps({"hook": kwargs.get("hook_text"), "trim": list(kwargs["trim"]) if kwargs.get("trim") else None}))
        return RenderResult(output=out, metadata=meta, words=0, hook=kwargs.get("hook_text"))


@pytest.fixture
def env(tmp_path: Path) -> tuple[TestClient, Store, FakePipeline, Path]:
    settings = Settings(CLIPPER_DATA_DIR=tmp_path, CLIPPER_OUTPUT_DIR=tmp_path / "out")
    out = tmp_path / "out"
    out.mkdir()
    store = Store(settings.db_path)
    clip = make_clip("c1", login="alanzoka")
    store.upsert_discovered(clip, 12.5, 2)
    video = out / "c1.mp4"
    video.write_bytes(b"0123456789")
    video.with_suffix(".json").write_text(json.dumps({
        "hook": "FUGA A PÉ?", "suggested_caption": "FUGA A PÉ?\n\n#gta6", "transcript": "corre corre",
        "trim": [3.0, 20.0],
    }))  # fmt: skip
    store.set_status(clip.key, ClipStatus.READY, output_path=str(video))
    catalog = StreamerCatalog(streamers=[Streamer(name="Alanzoka", platform=Platform.TWITCH, login="alanzoka")])
    fake = FakePipeline(Store(settings.db_path), out)
    app = create_app(settings, catalog, lambda: fake)  # type: ignore[arg-type, return-value]
    return TestClient(app), store, fake, video


def test_lists_ready_clip_with_metadata(env: tuple[TestClient, Store, FakePipeline, Path]) -> None:
    client, _, _, _ = env
    clips = client.get("/api/clips", params={"status": "ready"}).json()
    assert len(clips) == 1
    c = clips[0]
    assert (c["key"], c["hook"], c["has_video"], c["trim_start"]) == ("twitch:c1", "FUGA A PÉ?", True, 3.0)
    assert c["video_url"].startswith("/media/twitch:c1?v=")
    assert client.get("/api/clips", params={"q": "nada disso"}).json() == []
    assert len(client.get("/api/clips", params={"q": "corre"}).json()) == 1


def test_stats_counts_to_review_per_streamer(env: tuple[TestClient, Store, FakePipeline, Path]) -> None:
    client, _, _, _ = env
    stats = client.get("/api/stats").json()
    assert stats["by_status"]["ready"] == 1
    assert stats["streamers"][0] == {"platform": "twitch", "login": "alanzoka", "name": "Alanzoka", "to_review": 1}


def test_status_changes_and_validation(env: tuple[TestClient, Store, FakePipeline, Path]) -> None:
    client, store, _, _ = env
    r = client.post("/api/clips/twitch:c1/status", json={"status": "approved"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert store.get("twitch:c1").status == ClipStatus.APPROVED  # type: ignore[union-attr]
    assert client.post("/api/clips/twitch:c1/status", json={"status": "discovered"}).status_code == 422
    assert client.post("/api/clips/twitch:nope/status", json={"status": "approved"}).status_code == 404


def test_media_supports_range_requests(env: tuple[TestClient, Store, FakePipeline, Path]) -> None:
    client, _, _, _ = env
    r = client.get("/media/twitch:c1", headers={"Range": "bytes=2-5"})
    assert r.status_code == 206 and r.content == b"2345"
    assert "inline" in r.headers["content-disposition"]
    assert "attachment" in client.get("/media/twitch:c1?download=true").headers["content-disposition"]
    assert client.get("/media/twitch:nope").status_code == 404


def test_rerender_runs_in_background_with_trim_and_hook(env: tuple[TestClient, Store, FakePipeline, Path]) -> None:
    client, store, fake, _ = env
    r = client.post("/api/clips/twitch:c1/rerender", json={"hook": "novo gancho", "start": 2, "end": 12})
    assert r.status_code == 202
    for _ in range(100):
        if client.get("/api/jobs").json()[0]["state"] in ("done", "failed"):
            break
        time.sleep(0.02)
    job = client.get("/api/jobs").json()[0]
    assert job["state"] == "done", job
    assert fake.calls == [{"score": 12.5, "captions": True, "hook_text": "novo gancho", "trim": (2.0, 12.0)}]
    assert store.get("twitch:c1").status == ClipStatus.READY  # type: ignore[union-attr]


def test_rerender_validation(env: tuple[TestClient, Store, FakePipeline, Path]) -> None:
    client, _, _, _ = env
    assert client.post("/api/clips/twitch:c1/rerender", json={"start": 2}).status_code == 422
    assert client.post("/api/clips/twitch:c1/rerender", json={"start": 5, "end": 5.5}).status_code == 422
    assert client.post("/api/clips/twitch:c1/rerender", json={"start": 0, "end": 999}).status_code == 422


def test_index_and_static_are_served(env: tuple[TestClient, Store, FakePipeline, Path]) -> None:
    client, _, _, _ = env
    assert "Clipper" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_shift_words_rebases_and_clips_to_trim() -> None:
    words = [Word("a", 0.0, 1.0), Word("b", 2.5, 3.5), Word("c", 9.0, 11.0), Word("d", 12.0, 13.0)]
    assert shift_words(words, 3.0, 10.0) == [Word("b", 0.0, 0.5), Word("c", 6.0, 7.0)]

