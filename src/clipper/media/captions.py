"""TikTok-style captions: short uppercase chunks with the spoken word highlighted (ASS)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from clipper.media.transcribe import Word

PLAY_RES_X = 1080
PLAY_RES_Y = 1920

# ASS colors are &HAABBGGRR
WHITE = "&H00FFFFFF"
YELLOW = "&H0000E5FF"
BLACK = "&H00000000"


@dataclass(frozen=True, slots=True)
class CaptionStyle:
    font: str = "DejaVu Sans"
    font_size: int = 86
    outline: int = 7
    shadow: int = 2
    margin_v: int = 620          # distance from bottom; ~1/3 from the bottom by default
    max_words: int = 3
    max_chars: int = 18
    max_gap_s: float = 0.6       # a pause longer than this starts a new chunk
    hold_s: float = 0.25         # keep last word on screen a bit after it is spoken


def chunk_words(words: Sequence[Word], style: CaptionStyle) -> list[list[Word]]:
    chunks: list[list[Word]] = []
    current: list[Word] = []
    for w in words:
        if current:
            joined = " ".join(x.text for x in current) + " " + w.text
            gap = w.start - current[-1].end
            if len(current) >= style.max_words or len(joined) > style.max_chars or gap > style.max_gap_s:
                chunks.append(current)
                current = []
        current.append(w)
    if current:
        chunks.append(current)
    return chunks


def _ts(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    cs = int(round(seconds * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape(text: str) -> str:
    # Braces start override blocks and backslashes start escapes in ASS.
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(words: Sequence[Word], style: CaptionStyle | None = None) -> str:
    style = style or CaptionStyle()
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PLAY_RES_X}
PlayResY: {PLAY_RES_Y}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{style.font},{style.font_size},{WHITE},{WHITE},{BLACK},{BLACK},-1,0,0,0,100,100,0,0,1,{style.outline},{style.shadow},2,60,60,{style.margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines: list[str] = []
    chunks = chunk_words(words, style)
    for ci, chunk in enumerate(chunks):
        next_chunk_start = chunks[ci + 1][0].start if ci + 1 < len(chunks) else None
        tokens = [_escape(w.text.upper()) for w in chunk]
        for wi, w in enumerate(chunk):
            start = w.start
            if wi + 1 < len(chunk):
                end = chunk[wi + 1].start
            else:
                end = w.end + style.hold_s
                if next_chunk_start is not None:
                    end = min(end, next_chunk_start)
            if end <= start:
                end = start + 0.05
            parts = [
                f"{{\\c{YELLOW}}}{tok}{{\\c{WHITE}}}" if i == wi else tok
                for i, tok in enumerate(tokens)
            ]
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Default,,0,0,0,,{' '.join(parts)}")
    return header + "\n".join(lines) + ("\n" if lines else "")
