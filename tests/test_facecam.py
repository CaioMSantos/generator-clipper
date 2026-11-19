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


def _bright_detector(crop: Any) -> tuple[float, float] | None:
    if not crop.size or crop.max() != 255:
        return None
    ys, xs = np.nonzero(crop.max(axis=2) == 255)
    return (float(xs.mean()) / crop.shape[1], float(ys.mean()) / crop.shape[0])


def test_choose_candidate_threshold_and_ties() -> None:
    assert choose_candidate([0, 4], samples=6) == 1
    assert choose_candidate([3, 3], samples=6) == 0  # tie -> primary calibration
    assert choose_candidate([1, 1], samples=6) is None  # below 2/6
    assert choose_candidate([], samples=6) is None
    assert choose_candidate([5], samples=0) is None


def test_choose_candidate_prefers_best_framed_face_among_overlapping_boxes() -> None:
    # Both boxes see the face; the second frames it better.
    assert choose_candidate([6, 6], samples=6, misfit=[0.30, 0.05]) == 1
    # One hit less but much better framed still wins; two hits less does not.
    assert choose_candidate([6, 5], samples=6, misfit=[0.30, 0.05]) == 1
    assert choose_candidate([6, 4], samples=6, misfit=[0.30, 0.05]) == 0


def test_pick_facecam_picks_the_box_that_centers_the_face() -> None:
    f = np.zeros((100, 100, 3), dtype=np.uint8)
    f[50:60, 10:20] = 255  # face around y=55
    high = Rect(x=0.0, y=0.30, w=0.30, h=0.30)  # face lands at ~0.83 of the box (bottom)
    centered = Rect(x=0.0, y=0.40, w=0.30, h=0.30)  # face at ~0.5 of the box
    assert pick_facecam(Path("x.mp4"), [high, centered], _bright_detector, [f] * 6) == centered


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
