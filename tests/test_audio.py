from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from clipper.media import MediaError
from clipper.media import audio


def _fake_run(stdout: bytes, returncode: int = 0, stderr: bytes = b"") -> Any:
    def run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        assert "-ar" in cmd and cmd[cmd.index("-ar") + 1] == "16000"
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    return run


@pytest.fixture(autouse=True)
def _ffmpeg_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("clipper.media.audio.shutil.which", lambda _: "ffmpeg")


def test_load_audio_converts_s16le_to_float32(monkeypatch: pytest.MonkeyPatch) -> None:
    pcm = np.array([0, 16384, -32768, 32767], dtype=np.int16).tobytes()
    monkeypatch.setattr("clipper.media.audio.subprocess.run", _fake_run(pcm))
    out = audio.load_audio(Path("x.mp4"))
    assert out.dtype == np.float32
    np.testing.assert_allclose(out, [0.0, 0.5, -1.0, 32767 / 32768], rtol=1e-6)


def test_load_audio_ignores_trailing_odd_byte(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("clipper.media.audio.subprocess.run", _fake_run(b"\x00\x40\x01"))
    assert audio.load_audio(Path("x.mp4")).shape == (1,)


def test_load_audio_raises_on_ffmpeg_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("clipper.media.audio.subprocess.run", _fake_run(b"", 1, b"Invalid data"))
    with pytest.raises(MediaError, match="Invalid data"):
        audio.load_audio(Path("x.mp4"))


def test_load_audio_raises_without_audio_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("clipper.media.audio.subprocess.run", _fake_run(b""))
    with pytest.raises(MediaError, match="no audio"):
        audio.load_audio(Path("x.mp4"))


def test_load_audio_raises_without_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("clipper.media.audio.shutil.which", lambda _: None)
    with pytest.raises(MediaError, match="ffmpeg not found"):
        audio.load_audio(Path("x.mp4"))
