from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clipper.config import Platform, Rect, Settings, StackLayout, StreamerCatalog, load_catalog
from clipper.media.facecam import FacecamScore
from clipper.panel import app as panel_app
from clipper.panel.app import create_app
from clipper.panel.connections import CatalogEditor
from clipper.store import Store
from tests.conftest import make_clip

YAML = """\
streamers:
  # calibrated by hand, keep this comment
  - name: Davy Jones
    platform: kick
    login: davyjones
    layout:
      mode: stack
      facecam: { x: 0.0, y: 0.0, w: 0.2, h: 0.2 }   # left corner
      facecam_ratio: 0.30
  - name: Gaules
    platform: kick
    login: gaules
    layout:
      mode: blur
"""


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path, Settings, Store]:
    yaml_path = tmp_path / "streamers.yaml"
    yaml_path.write_text(YAML, encoding="utf-8")
    settings = Settings(CLIPPER_DATA_DIR=tmp_path / "data", CLIPPER_STREAMERS_FILE=yaml_path)
    store = Store(settings.db_path)
    for i, cid in enumerate(["clip_A", "clip_B"]):
        store.upsert_discovered(make_clip(cid, login="davyjones", platform=Platform.KICK, age_min=10 + i), 1.0, 1)
    settings.downloads_dir.mkdir(parents=True, exist_ok=True)
    (settings.downloads_dir / "kick_clip_B.mp4").write_bytes(b"fake")  # only B is downloaded

    def fake_frame(source: Path, output: Path, at_s: float = 1.0) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"\x89PNG\r\n\x1a\nfake-" + source.name.encode())
        return output

    def fake_download(url: str, dest_dir: Path, basename: str) -> Path:
        p = dest_dir / f"{basename}.mp4"
        p.write_bytes(b"downloaded")
        return p

    monkeypatch.setattr(panel_app, "extract_frame", fake_frame)
    monkeypatch.setattr(panel_app, "download_clip", fake_download)
    app = create_app(
        settings, StreamerCatalog(streamers=[]), lambda: None,  # type: ignore[arg-type, return-value]
        env_path=tmp_path / ".env",
    )  # fmt: skip
    return TestClient(app, headers={"X-Clipper-Panel": "1"}), yaml_path, settings, store


def test_clips_list_puts_downloaded_first(ctx: tuple[TestClient, Path, Settings, Store]) -> None:
    c, _, _, _ = ctx
    clips = c.get("/api/streamers/kick/davyjones/clips").json()
    assert [x["key"] for x in clips] == ["kick:clip_B", "kick:clip_A"]
    assert [x["downloaded"] for x in clips] == [True, False]
    assert c.get("/api/streamers/kick/nobody/clips").status_code == 404


def test_frame_uses_download_or_fetches_it(ctx: tuple[TestClient, Path, Settings, Store]) -> None:
    c, _, settings, _ = ctx
    r = c.get("/api/streamers/kick/davyjones/frame", params={"clip": "kick:clip_B", "at": 3})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content.endswith(b"kick_clip_B.mp4")
    r = c.get("/api/streamers/kick/davyjones/frame", params={"clip": "kick:clip_A", "at": 3})
    assert r.status_code == 200 and (settings.downloads_dir / "kick_clip_A.mp4").is_file()
    wrong = c.get("/api/streamers/kick/gaules/frame", params={"clip": "kick:clip_A"})
    assert wrong.status_code == 404  # clip belongs to another streamer


def test_get_and_put_layout_keeps_yaml_comments(ctx: tuple[TestClient, Path, Settings, Store]) -> None:
    c, yaml_path, _, _ = ctx
    got = c.get("/api/streamers/kick/davyjones/layout").json()
    assert got["mode"] == "stack" and got["facecam"]["w"] == 0.2
    body = {
        "mode": "stack",
        "facecam": {"x": 0.01, "y": 0.02, "w": 0.21, "h": 0.21},
        "facecam_alternates": [{"x": 0.78, "y": 0.1, "w": 0.21, "h": 0.21}],
        "gameplay": {"x": 0.25, "y": 0.0, "w": 0.5, "h": 1.0},
        "facecam_ratio": 0.33,
    }
    r = c.put("/api/streamers/kick/davyjones/layout", json=body)
    assert r.status_code == 200, r.text
    layout = load_catalog(yaml_path).find(Platform.KICK, "davyjones").layout  # type: ignore[union-attr]
    assert isinstance(layout, StackLayout)
    assert layout.facecam_alternates == [Rect(x=0.78, y=0.1, w=0.21, h=0.21)]
    assert layout.gameplay.w == 0.5 and layout.facecam_ratio == 0.33
    text = yaml_path.read_text(encoding="utf-8")
    assert "# calibrated by hand, keep this comment" in text
    assert "facecam: {x: 0.01, y: 0.02, w: 0.21, h: 0.21}" in text  # flow style, one line

    # switch to blur drops the stack-only keys
    assert c.put("/api/streamers/kick/davyjones/layout", json={"mode": "blur"}).status_code == 200
    blur = load_catalog(yaml_path).find(Platform.KICK, "davyjones").layout  # type: ignore[union-attr]
    assert blur.mode == "blur" and "facecam_alternates" not in yaml_path.read_text(encoding="utf-8")


def test_put_layout_validation(ctx: tuple[TestClient, Path, Settings, Store]) -> None:
    c, yaml_path, _, _ = ctx
    before = yaml_path.read_text(encoding="utf-8")
    no_cam = c.put("/api/streamers/kick/davyjones/layout", json={"mode": "stack"})
    assert no_cam.status_code == 422
    outside = c.put(
        "/api/streamers/kick/davyjones/layout",
        json={"mode": "stack", "facecam": {"x": 0.9, "y": 0.0, "w": 0.3, "h": 0.2}},
    )
    assert outside.status_code == 422  # rect must fit in the frame
    assert yaml_path.read_text(encoding="utf-8") == before


def test_detect_reports_the_winning_position(
    ctx: tuple[TestClient, Path, Settings, Store], monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _, _, _ = ctx
    seen: dict[str, Any] = {}

    def fake_score(source: Path, candidates: list[Rect], *_: Any) -> FacecamScore:
        seen["n"] = len(candidates)
        return FacecamScore(samples=6, hits=[0, 5], misfit=[float("inf"), 0.1], chosen=1)

    monkeypatch.setattr(panel_app, "score_candidates", fake_score)
    body = {
        "clip": "kick:clip_B",
        "layout": {
            "mode": "stack",
            "facecam": {"x": 0.0, "y": 0.0, "w": 0.2, "h": 0.2},
            "facecam_alternates": [{"x": 0.8, "y": 0.0, "w": 0.2, "h": 0.2}],
        },
    }
    r = c.post("/api/streamers/kick/davyjones/detect", json=body).json()
    assert (r["chosen"], r["hits"], seen["n"]) == (1, [0, 5], 2)
    assert "CAM 2" in r["message"]
    blur = c.post("/api/streamers/kick/davyjones/detect", json={"clip": "kick:clip_B", "layout": {"mode": "blur"}})
    assert blur.status_code == 422


def test_store_recent_for_streamer(tmp_path: Path) -> None:
    store = Store(tmp_path / "q.sqlite3")
    store.upsert_discovered(make_clip("old", login="davyjones", platform=Platform.KICK, age_min=60), 1.0, 1)
    store.upsert_discovered(make_clip("new", login="davyjones", platform=Platform.KICK, age_min=5), 1.0, 1)
    store.upsert_discovered(make_clip("tw", login="davyjones", platform=Platform.TWITCH, age_min=1), 1.0, 1)
    rows = store.recent_for_streamer(Platform.KICK, "DavyJones")
    assert [r.clip.id for r in rows] == ["new", "old"]


def test_set_layout_on_blur_streamer_creates_stack_keys(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text(YAML, encoding="utf-8")
    CatalogEditor(p).set_layout(
        Platform.KICK, "gaules",
        StackLayout(mode="stack", facecam=Rect(x=0.0, y=0.4, w=0.2, h=0.2), facecam_ratio=0.3),
    )  # fmt: skip
    layout = load_catalog(p).find(Platform.KICK, "gaules").layout  # type: ignore[union-attr]
    assert isinstance(layout, StackLayout) and layout.facecam.y == 0.4
