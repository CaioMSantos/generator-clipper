from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from clipper.config import BlurLayout, Rect, Settings, SlotSpec, StackLayout, StreamerCatalog, Template
from clipper.media import facecam
from clipper.media.compose import (
    BUILTIN_IDS, BUILTIN_TEMPLATES, FALLBACK_ID, Composition, TemplateStore, build_compose_graph,
    builtin, composition_for, legacy_template, slot_box, write_slot_assets,
)  # fmt: skip
from clipper.media.render import build_filtergraph, hook_center_y
from clipper.panel.app import create_app
from clipper.pipeline import Pipeline
from clipper.store import Store

CAM = Rect(x=0.78, y=0.1, w=0.2, h=0.2)


def _custom(tid: str = "meu-molde") -> Template:
    return Template(
        id=tid, name="Meu molde",
        slots=[
            SlotSpec(kind="game", dst=Rect(x=0, y=0, w=1, h=1)),
            SlotSpec(kind="cam", dst=Rect(x=0.1, y=0.1, w=0.4, h=0.2), shape="circle", border_px=6, border_color="#ff0000"),
        ],
        hook_y=0.4,
    )  # fmt: skip


# ---- templates & composition --------------------------------------------------------


def test_builtins_are_valid_and_unique() -> None:
    assert len(BUILTIN_IDS) == len(BUILTIN_TEMPLATES) >= 6
    assert FALLBACK_ID in BUILTIN_IDS and not builtin(FALLBACK_ID).needs_cam


def test_legacy_template_matches_classic_layouts() -> None:
    stack = legacy_template(StackLayout(mode="stack", facecam=CAM, facecam_ratio=0.3))
    assert [s.kind for s in stack.slots] == ["cam", "game"] and stack.slots[0].dst.h == 0.3
    assert legacy_template(BlurLayout()).id == FALLBACK_ID


def test_composition_falls_back_without_camera() -> None:
    comp = composition_for(StackLayout(mode="stack", facecam=CAM), builtin("pip-circle"), cam_src=None)
    assert comp.template.id == FALLBACK_ID
    ok = composition_for(StackLayout(mode="stack", facecam=CAM), builtin("pip-circle"), cam_src=CAM)
    assert ok.template.id == "pip-circle" and ok.cam_src == CAM
    assert ok.hook_center_px == round(builtin("pip-circle").hook_y * 1920)
    assert hook_center_y(ok) == ok.hook_center_px


def test_circle_slot_is_square_in_pixels() -> None:
    x, y, w, h = slot_box(SlotSpec(kind="cam", dst=Rect(x=0.5, y=0, w=0.5, h=0.2), shape="circle"))
    assert w == h == 384 and x + w <= 1080


def test_compose_graph_wires_masks_borders_and_fit() -> None:
    comp = Composition(_custom(), CAM)
    graph, assets = build_compose_graph(comp, first_asset_input=2)
    assert [(a.kind, a.slot_index) for a in assets] == [("mask", 1), ("border", 1)]
    assert "[2:v]format=gray[m1]" in graph and "alphamerge=shortest=1" in graph
    assert "[3:v]overlay=" in graph and graph.endswith("[base]")
    blur = Composition(builtin(FALLBACK_ID), None)
    g, a = build_compose_graph(blur, first_asset_input=1)
    assert a == [] and "gblur" in g and "force_original_aspect_ratio=decrease" in g and "(1920-h)/2" in g


def test_full_filtergraph_appends_captions_and_hook_after_template() -> None:
    from clipper.media.render import HookOverlay

    g = build_filtergraph(Composition(_custom(), CAM), True, hook=HookOverlay(top_y=700), with_fontsdir=True)
    # hook is input 1, so slot assets start at 2
    assert "[2:v]format=gray" in g and "ass=captions.ass:fontsdir=fonts" in g and "[1:v]format=rgba,fade" in g
    assert g.endswith("[v]")


def test_slot_assets_are_drawn_with_transparency(tmp_path: Path) -> None:
    assets = write_slot_assets(Composition(_custom(), CAM), tmp_path)
    mask = Image.open(tmp_path / assets[0].filename)
    border = Image.open(tmp_path / assets[1].filename)
    assert mask.mode == "L" and mask.size == border.size
    assert mask.getpixel((0, 0)) == 0 and mask.getpixel((mask.width // 2, mask.height // 2)) == 255
    center = border.getpixel((border.width // 2, border.height // 2))
    top = border.getpixel((border.width // 2, 2))
    assert isinstance(center, tuple) and center[3] == 0  # hole in the middle
    assert isinstance(top, tuple) and top[0] > 200  # red ring at the top


# ---- store ----------------------------------------------------------------------------


def test_template_store_crud_and_builtin_protection(tmp_path: Path) -> None:
    store = TemplateStore(tmp_path / "templates.yaml")
    assert {t.id for t in store.all()} == set(BUILTIN_IDS)
    store.save(_custom())
    assert store.get("meu-molde") is not None and store.get("meu-molde").hook_y == 0.4  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        store.save(_custom("stack-30"))
    with pytest.raises(ValueError):
        store.delete("stack-30")
    store.delete("meu-molde")
    assert store.get("meu-molde") is None
    with pytest.raises(KeyError):
        store.delete("meu-molde")


def test_template_store_ignores_broken_file(tmp_path: Path) -> None:
    p = tmp_path / "templates.yaml"
    p.write_text("templates: [{id: X, name: bad}]", encoding="utf-8")
    assert {t.id for t in TemplateStore(p).all()} == set(BUILTIN_IDS)


# ---- pipeline -------------------------------------------------------------------------


def _pipe(tmp_path: Path) -> Pipeline:
    settings = Settings(CLIPPER_DATA_DIR=tmp_path, CLIPPER_TEMPLATES_FILE=tmp_path / "t.yaml")
    return Pipeline(
        settings, StreamerCatalog(streamers=[]), Store(settings.db_path), [], transcriber=None,  # type: ignore[arg-type]
        face_detector=lambda crop: (0.5, 0.4),
    )  # fmt: skip


def test_pipeline_resolves_template_with_detected_camera(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pipe = _pipe(tmp_path)
    layout = StackLayout(mode="stack", facecam=Rect(x=0, y=0, w=0.2, h=0.2), facecam_alternates=[CAM])
    monkeypatch.setattr(facecam, "sample_frames", lambda _src: [object()] * 6)
    monkeypatch.setattr(facecam, "_crop", lambda f, r: r)
    # detector "sees" a face only in the alternate position
    pipe.face_detector = lambda crop: (0.5, 0.4) if crop == CAM else None
    comp = pipe._resolve(Path("x.mp4"), layout, "pip-circle")
    assert isinstance(comp, Composition) and comp.template.id == "pip-circle" and comp.cam_src == CAM

    pipe.face_detector = lambda crop: None  # no face anywhere -> fallback template
    comp = pipe._resolve(Path("x.mp4"), layout, "pip-circle")
    assert isinstance(comp, Composition) and comp.template.id == FALLBACK_ID

    # no template -> classic path untouched; unknown template -> classic too
    assert isinstance(pipe._resolve(Path("x.mp4"), BlurLayout(), None), BlurLayout)
    assert isinstance(pipe._resolve(Path("x.mp4"), BlurLayout(), "nope"), BlurLayout)


# ---- API ------------------------------------------------------------------------------

YAML = """\
streamers:
  - name: Davy Jones
    platform: kick
    login: davyjones
    template: pip-circle
    layout:
      mode: blur
  - name: Gaules
    platform: kick
    login: gaules
    layout:
      mode: blur
"""


@pytest.fixture
def client(tmp_path: Path) -> tuple[TestClient, Path]:
    yaml_path = tmp_path / "streamers.yaml"
    yaml_path.write_text(YAML, encoding="utf-8")
    settings = Settings(
        CLIPPER_DATA_DIR=tmp_path / "data", CLIPPER_STREAMERS_FILE=yaml_path,
        CLIPPER_TEMPLATES_FILE=tmp_path / "templates.yaml",
    )  # fmt: skip
    app = create_app(
        settings, StreamerCatalog(streamers=[]), lambda: None,  # type: ignore[arg-type, return-value]
        env_path=tmp_path / ".env",
    )  # fmt: skip
    return TestClient(app, headers={"X-Clipper-Panel": "1"}), yaml_path


def test_templates_api(client: tuple[TestClient, Path]) -> None:
    c, _ = client
    listed = {t["id"]: t for t in c.get("/api/templates").json()}
    assert listed["pip-circle"]["builtin"] is True and listed["pip-circle"]["used_by"] == ["Davy Jones (kick)"]
    body: dict[str, Any] = _custom().model_dump(mode="json")
    assert c.put("/api/templates/meu-molde", json=body).status_code == 200
    assert any(t["id"] == "meu-molde" and not t["builtin"] for t in c.get("/api/templates").json())
    assert c.put("/api/templates/outro", json=body).status_code == 422  # id mismatch
    body_builtin = {**body, "id": "stack-30"}
    assert c.put("/api/templates/stack-30", json=body_builtin).status_code == 409
    bad = {**body, "slots": []}
    assert c.put("/api/templates/meu-molde", json=bad).status_code == 422
    assert c.delete("/api/templates/pip-circle").status_code == 409  # in use (and builtin)
    assert c.delete("/api/templates/meu-molde").status_code == 200
    assert c.delete("/api/templates/meu-molde").status_code == 404


def test_streamer_template_assignment(client: tuple[TestClient, Path]) -> None:
    c, yaml_path = client
    r = c.patch("/api/streamers/kick/gaules", json={"template": "stack-50"})
    assert r.status_code == 200 and next(s for s in r.json() if s["login"] == "gaules")["template"] == "stack-50"
    assert "template: stack-50" in yaml_path.read_text(encoding="utf-8")
    assert c.patch("/api/streamers/kick/gaules", json={"template": "nao-existe"}).status_code == 404
    r = c.patch("/api/streamers/kick/gaules", json={"clear_template": True})
    assert next(s for s in r.json() if s["login"] == "gaules")["template"] is None
