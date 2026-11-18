from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from clipper.config import BlurLayout, Rect, Settings, StackLayout, StreamerCatalog
from clipper.media import facecam
from clipper.media.facecam import choose_candidate, pick_facecam
from clipper.pipeline import Pipeline
from clipper.store import Store

LEFT = Rect(x=0.0, y=0.0, w=0.25, h=0.25)
RIGHT = Rect(x=0.75, y=0.0, w=0.25, h=0.25)


def _frame(face_at: str | None) -> Any:
    """100x100 black frame with a white square where the 'face' is."""
    f = np.zeros((100, 100, 3), dtype=np.uint8)
    if face_at == "left":
        f[5:20, 5:20] = 255
    elif face_at == "right":
        f[5:20, 80:95] = 255
    return f


def _bright_detector(crop: Any) -> bool:
    return bool(crop.size and crop.max() == 255)


def test_choose_candidate_threshold_and_ties() -> None:
    assert choose_candidate([0, 4], samples=6) == 1
    assert choose_candidate([3, 3], samples=6) == 0  # tie -> primary calibration
    assert choose_candidate([1, 1], samples=6) is None  # below 2/6
    assert choose_candidate([], samples=6) is None
    assert choose_candidate([5], samples=0) is None


def test_pick_facecam_follows_the_face() -> None:
    frames = [_frame("right")] * 4 + [_frame(None)] * 2
    assert pick_facecam(Path("x.mp4"), [LEFT, RIGHT], _bright_detector, frames) == RIGHT
    assert pick_facecam(Path("x.mp4"), [LEFT, RIGHT], _bright_detector, [_frame(None)] * 6) is None
    assert pick_facecam(Path("x.mp4"), [LEFT, RIGHT], _bright_detector, []) is None


@pytest.fixture
def pipe(tmp_path: Path) -> Pipeline:
    settings = Settings(CLIPPER_DATA_DIR=tmp_path)
    return Pipeline(
        settings, StreamerCatalog(streamers=[]), Store(settings.db_path), [], transcriber=None,  # type: ignore[arg-type]
        face_detector=_bright_detector,
    )


def test_resolve_layout_swaps_facecam_or_falls_back_to_blur(
    pipe: Pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = StackLayout(mode="stack", facecam=LEFT, facecam_alternates=[RIGHT])

    monkeypatch.setattr(facecam, "sample_frames", lambda _src: [_frame("right")] * 6)
    resolved = pipe._resolve_layout(Path("x.mp4"), layout)
    assert isinstance(resolved, StackLayout) and resolved.facecam == RIGHT

    monkeypatch.setattr(facecam, "sample_frames", lambda _src: [_frame(None)] * 6)
    assert isinstance(pipe._resolve_layout(Path("x.mp4"), layout), BlurLayout)

    assert pipe._resolve_layout(Path("x.mp4"), BlurLayout()) == BlurLayout()  # untouched


def test_resolve_layout_keeps_calibration_when_detection_crashes(
    pipe: Pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_src: Path) -> list[Any]:
        raise RuntimeError("codec")

    monkeypatch.setattr(facecam, "sample_frames", boom)
    layout = StackLayout(mode="stack", facecam=LEFT)
    assert pipe._resolve_layout(Path("x.mp4"), layout) == layout
