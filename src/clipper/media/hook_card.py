"""Renders the hook title as a PNG: black bold text in a white rounded box."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from clipper.media import MediaError


@dataclass(frozen=True, slots=True)
class HookCardStyle:
    font_size: int = 66
    max_box_w: int = 860       # out of 1080
    min_box_w: int = 620
    pad_x: int = 40
    pad_y: int = 30
    line_gap: int = 8
    radius: int = 22
    max_lines: int = 3


class _Fits:
    def __init__(self, font: ImageFont.FreeTypeFont) -> None:
        self._font = font

    def width(self, text: str) -> int:
        left, _, right, _ = self._font.getbbox(text)
        return int(right - left)

    def __call__(self, text: str, max_w: int) -> bool:
        return self.width(text) <= max_w


def wrap_lines(words: list[str], fits: _Fits, max_w: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and not fits(candidate, max_w):
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def render_hook_card(text: str, font_path: Path, output: Path, style: HookCardStyle | None = None) -> Path:
    """Writes an RGBA PNG sized to the box. Shrinks the font if the text needs too many lines."""
    style = style or HookCardStyle()
    words = text.split()
    if not words:
        raise MediaError("empty hook text")
    text_max_w = style.max_box_w - 2 * style.pad_x

    size = style.font_size
    while True:
        try:
            font = ImageFont.truetype(str(font_path), size)
        except OSError as exc:
            raise MediaError(f"cannot load font {font_path}: {exc}") from exc
        fits = _Fits(font)
        lines = wrap_lines(words, fits, text_max_w)
        too_wide = any(fits.width(ln) > text_max_w for ln in lines)  # a single huge word
        if (len(lines) <= style.max_lines and not too_wide) or size <= 36:
            break
        size -= 4

    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    text_w = max(fits.width(ln) for ln in lines)
    box_w = min(max(text_w + 2 * style.pad_x, style.min_box_w), style.max_box_w)
    box_h = len(lines) * line_h + (len(lines) - 1) * style.line_gap + 2 * style.pad_y

    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, box_w - 1, box_h - 1), radius=style.radius, fill=(255, 255, 255, 255))
    y = style.pad_y
    for ln in lines:
        x = (box_w - fits.width(ln)) // 2 - font.getbbox(ln)[0]
        draw.text((x, y), ln, font=font, fill=(0, 0, 0, 255))
        y += line_h + style.line_gap
    output.parent.mkdir(parents=True, exist_ok=True)
    img.save(output, format="PNG")
    return output
