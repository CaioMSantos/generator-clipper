"""Per-clip facecam location: streamers move their camera between scenes mid-stream.

Each streamer lists candidate facecam rects; we sample a few frames of the clip, run a face
detector inside each candidate and keep the one where a face shows up most often. No match
(e.g. a "just chatting" scene or a cutscene with the camera hidden) returns None so the caller
can fall back to the blur layout instead of rendering a top band without the streamer.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clipper.config import Rect

log = logging.getLogger(__name__)

SAMPLES = 6
MIN_HIT_RATIO = 0.34  # a face in at least ~2 of 6 frames (people look down, turn, blink)

# Where a well-framed webcam face sits inside the cam box (normalized center).
IDEAL_FACE_CENTER = (0.5, 0.38)

# A detector gets one BGR crop (numpy array) and returns the normalized center (x, y) of the
# biggest face in it, or None.
FaceDetector = Callable[[Any], "tuple[float, float] | None"]


def choose_candidate(
    hits: Sequence[int],
    samples: int,
    misfit: Sequence[float] | None = None,
    min_ratio: float = MIN_HIT_RATIO,
) -> int | None:
    """Index of the best candidate, or None if no candidate has a face often enough.

    Candidates overlap (a streamer nudges the cam a few % between scenes), so several may
    "see" the face. Among those within one hit of the best, the one where the face is framed
    closest to IDEAL_FACE_CENTER (lowest `misfit`) wins; remaining ties go to the earlier one.
    """
    if samples <= 0 or not hits:
        return None
    needed = max(1, math.ceil(samples * min_ratio))
    top = max(hits)
    if top < needed:
        return None
    contenders = [i for i, h in enumerate(hits) if h >= max(top - 1, needed)]
    if misfit is None:
        return max(contenders, key=lambda i: (hits[i], -i))
    return min(contenders, key=lambda i: (round(misfit[i], 3), -hits[i], i))


def _misfit(center: tuple[float, float]) -> float:
    return abs(center[0] - IDEAL_FACE_CENTER[0]) + abs(center[1] - IDEAL_FACE_CENTER[1])


def _crop(frame: Any, rect: Rect) -> Any:
    h, w = frame.shape[:2]
    x0, y0 = int(rect.x * w), int(rect.y * h)
    x1, y1 = min(int((rect.x + rect.w) * w), w), min(int((rect.y + rect.h) * h), h)
    return frame[y0:y1, x0:x1]


def haar_detector() -> FaceDetector:
    import cv2  # heavy import, keep it lazy

    cascade_dir: str = getattr(cv2, "data").haarcascades  # cv2.data is missing from the stubs
    frontal = cv2.CascadeClassifier(cascade_dir + "haarcascade_frontalface_default.xml")
    # Streamers look at a second monitor or down at the keyboard: 3/4 and side views are common.
    profile = cv2.CascadeClassifier(cascade_dir + "haarcascade_profileface.xml")
    if frontal.empty() or profile.empty():
        raise RuntimeError("could not load OpenCV Haar face cascades")

    def detect(crop: Any) -> tuple[float, float] | None:
        if crop.size == 0:
            return None
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]
        # A webcam face fills a good part of its box; tiny detections are in-game noise.
        min_side = max(24, int(min(h, w) * 0.18))
        size = (min_side, min_side)
        faces = list(frontal.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=size))
        if not faces:
            faces = list(profile.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=size))
        if not faces:  # the profile cascade only knows one side; mirror for the other
            mirrored = profile.detectMultiScale(cv2.flip(gray, 1), scaleFactor=1.1, minNeighbors=5, minSize=size)
            faces = [(w - int(x) - int(fw), y, fw, fh) for x, y, fw, fh in mirrored]
        if not faces:
            return None
        fx, fy, fw, fh = max(faces, key=lambda f: int(f[2]) * int(f[3]))
        return ((int(fx) + int(fw) / 2) / w, (int(fy) + int(fh) / 2) / h)

    return detect


def sample_frames(source: Path, samples: int = SAMPLES) -> list[Any]:
    """Evenly spaced frames (skipping the first/last 10%, often transitions)."""
    import cv2

    cap = cv2.VideoCapture(str(source))
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total <= 0:
            return []
        start, end = int(total * 0.1), int(total * 0.9)
        step = max((end - start) // samples, 1)
        frames: list[Any] = []
        for i in range(samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, start + i * step)
            ok, frame = cap.read()
            if ok and frame is not None:
                frames.append(frame)
        return frames
    finally:
        cap.release()


@dataclass(frozen=True)
class FacecamScore:
    """Per-candidate detection result (also shown by the panel's calibration editor)."""

    samples: int
    hits: list[int]
    misfit: list[float]
    chosen: int | None


def score_candidates(
    source: Path,
    candidates: Sequence[Rect],
    detector: FaceDetector | None = None,
    frames: Sequence[Any] | None = None,
) -> FacecamScore:
    frames = list(frames) if frames is not None else sample_frames(source)
    if not candidates or not frames:
        if not frames:
            log.warning("facecam: could not read frames from %s", source.name)
        return FacecamScore(samples=len(frames), hits=[0] * len(candidates), misfit=[math.inf] * len(candidates), chosen=None)
    detect = detector or haar_detector()
    hits: list[int] = []
    misfit: list[float] = []
    for rect in candidates:
        centers = [c for c in (detect(_crop(f, rect)) for f in frames) if c is not None]
        hits.append(len(centers))
        misfit.append(sum(_misfit(c) for c in centers) / len(centers) if centers else math.inf)
    chosen = choose_candidate(hits, len(frames), misfit)
    log.info(
        "facecam %s: hits=%s/%d misfit=%s -> %s",
        source.name, hits, len(frames), [round(m, 2) for m in misfit], chosen,
    )  # fmt: skip
    return FacecamScore(samples=len(frames), hits=hits, misfit=misfit, chosen=chosen)


def pick_facecam(
    source: Path,
    candidates: Sequence[Rect],
    detector: FaceDetector | None = None,
    frames: Sequence[Any] | None = None,
) -> Rect | None:
    """Returns the candidate rect that contains the streamer's face, or None."""
    score = score_candidates(source, candidates, detector, frames)
    return candidates[score.chosen] if score.chosen is not None else None
