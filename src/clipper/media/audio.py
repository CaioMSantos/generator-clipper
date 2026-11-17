"""Audio decoding via the ffmpeg CLI.

faster-whisper's own decoder depends on PyAV, whose API changes between releases
(e.g. PyAV 19 dropped `metadata_errors`, which faster-whisper 1.2 still passes).
ffmpeg is already a hard requirement for rendering, so decode with it instead.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import numpy.typing as npt

from clipper.media import MediaError

SAMPLE_RATE = 16_000
DECODE_TIMEOUT_S = 300


def load_audio(media: Path, sample_rate: int = SAMPLE_RATE) -> npt.NDArray[np.float32]:
    """Returns mono float32 PCM in [-1, 1] at `sample_rate`, as whisper expects."""
    exe = shutil.which("ffmpeg")
    if exe is None:
        raise MediaError("ffmpeg not found in PATH")
    cmd = [
        exe, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(media),
        "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-",
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=DECODE_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"audio decode timed out after {DECODE_TIMEOUT_S}s") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()[-500:]
        raise MediaError(f"audio decode failed for {media.name}: {err}")
    if len(proc.stdout) < 2:
        raise MediaError(f"no audio stream in {media.name}")
    pcm = np.frombuffer(proc.stdout[: len(proc.stdout) // 2 * 2], dtype=np.int16)
    return (pcm.astype(np.float32) / 32768.0).astype(np.float32)
