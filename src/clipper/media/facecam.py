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
from pathlib import Path
from typing import Any

from clipper.config import Rect

log = logging.getLogger(__name__)

SAMPLES = 6
MIN_HIT_RATIO = 0.34  # a face in at least ~2 of 6 frames (people look down, turn, blink)

# A detector gets one BGR crop (numpy array) and says whether it contains a face.
FaceDetector = Callable[[Any], bool]


def choose_candidate(hits: Sequence[int], samples: int, min_ratio: float = MIN_HIT_RATIO) -> int | None:
    """Index of the candidate with most face hits, or None if none reaches the threshold.

    Ties go to the earlier candidate (the primary calibration comes first).
    """
    if samples <= 0 or not hits:
        return None
    needed = max(1, math.ceil(samples * min_ratio))
    best = max(range(len(hits)), key=lambda i: (hits[i], -i))
    return best if hits[best] >= needed else None


def _crop(frame: Any, rect: Rect) -> Any:
    h, w = frame.shape[:2]
    x0, y0 = int(rect.x * w), int(rect.y * h)
    x1, y1 = min(int((rect.x + rect.w) * w), w), min(int((rect.y + rect.h) * h), h)
    return frame[y0:y1, x0:x1]


def haar_detector() -> FaceDetector:
    import cv2  # heavy import, keep it lazy

    cascade_dir: str = getattr(cv2, "data").haarcascades  # cv2.data is missing from the stubs
    cascade = cv2.CascadeClassifier(cascade_dir + "haarcascade_frontalface_default.xml")
    if cascade.empty():
        raise RuntimeError("could not load OpenCV Haar face cascade")

    def detect(crop: Any) -> bool:
        if crop.size == 0:
            return False
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        # A webcam face fills a good part of its box; tiny detections are in-game noise.
        min_side = max(24, int(min(gray.shape[:2]) * 0.18))
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(min_side, min_side))
        return len(faces) > 0

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


def pick_facecam(
    source: Path,
    candidates: Sequence[Rect],
    detector: FaceDetector | None = None,
    frames: Sequence[Any] | None = None,
) -> Rect | None:
    """Returns the candidate rect that contains the streamer's face, or None."""
    if not candidates:
        return None
    frames = list(frames) if frames is not None else sample_frames(source)
    if not frames:
        log.warning("facecam: could not read frames from %s", source.name)
        return None
    detect = detector or haar_detector()
    hits = [sum(1 for f in frames if detect(_crop(f, rect))) for rect in candidates]
    chosen = choose_candidate(hits, len(frames))
    log.info("facecam %s: hits=%s/%d -> %s", source.name, hits, len(frames), chosen)
    return candidates[chosen] if chosen is not None else None
