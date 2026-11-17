"""Word-level transcription with faster-whisper (CPU, int8)."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from clipper.media import MediaError

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    start: float
    end: float


class Transcriber(Protocol):
    def transcribe(self, media: Path) -> list[Word]: ...


class WhisperTranscriber:
    """Lazy-loads the model on first use (model load takes a few seconds)."""

    def __init__(self, model_size: str = "small", language: str = "pt") -> None:
        self._model_size = model_size
        self._language = language
        self._model: Any = None
        self._lock = threading.Lock()

    def _get_model(self) -> Any:
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel  # heavy import, keep it lazy

                log.info("loading whisper model '%s' (cpu/int8)", self._model_size)
                self._model = WhisperModel(self._model_size, device="cpu", compute_type="int8")
            return self._model

    def transcribe(self, media: Path) -> list[Word]:
        try:
            segments, _info = self._get_model().transcribe(
                str(media),
                language=self._language,
                word_timestamps=True,
                vad_filter=True,
                beam_size=5,
                condition_on_previous_text=False,  # reduces hallucinated repeats on noisy game audio
            )
            words: list[Word] = []
            for seg in segments:
                for w in seg.words or []:
                    text = str(w.word).strip()
                    if text:
                        words.append(Word(text=text, start=float(w.start), end=float(w.end)))
            return words
        except Exception as exc:  # faster-whisper/av raise a variety of exception types
            raise MediaError(f"transcription failed for {media.name}: {exc}") from exc
