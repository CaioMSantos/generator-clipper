"""Font discovery shared by the hook card (Pillow) and the captions (libass)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import ImageFont

from clipper.media import MediaError

# Preferred bold sans faces, first match wins. Bundled fonts live in `fonts_dir`
# (assets/fonts); the system paths are fallbacks for Docker (DejaVu) and Windows (Arial).
BUNDLED_PREFERENCE = ("Montserrat-ExtraBold.ttf", "Montserrat-Bold.ttf", "Inter-Bold.ttf")
SYSTEM_FALLBACKS = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("C:/Windows/Fonts/arialbd.ttf"),
)


@dataclass(frozen=True, slots=True)
class FontSpec:
    path: Path
    family: str  # what libass matches against (`Fontname` in the ASS style)


def resolve_font(fonts_dir: Path) -> FontSpec:
    candidates = [fonts_dir / name for name in BUNDLED_PREFERENCE]
    if fonts_dir.is_dir():
        candidates += sorted(fonts_dir.glob("*.ttf"))
    candidates += list(SYSTEM_FALLBACKS)
    for path in candidates:
        if path.is_file():
            try:
                family = ImageFont.truetype(str(path), 10).getname()[0]
            except OSError:
                continue
            if family:
                return FontSpec(path=path, family=family)
    raise MediaError(f"no usable bold font found (looked in {fonts_dir} and system paths)")
