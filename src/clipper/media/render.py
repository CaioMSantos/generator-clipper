"""ffmpeg reframing to 1080x1920 with burned captions."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from clipper.config import BlurLayout, Layout, Rect, StackLayout
from clipper.media import MediaError

log = logging.getLogger(__name__)

OUT_W = 1080
OUT_H = 1920
FFMPEG_TIMEOUT_S = 600
CAPTIONS_FILENAME = "captions.ass"


def _crop(r: Rect) -> str:
    # Even dimensions keep libx264/yuv420p happy.
    return (
        f"crop=trunc(iw*{r.w}/2)*2:trunc(ih*{r.h}/2)*2:"
        f"trunc(iw*{r.x}/2)*2:trunc(ih*{r.y}/2)*2"
    )


def _fill(w: int, h: int) -> str:
    """Scale to cover WxH, then center-crop to exactly WxH."""
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"


def build_filtergraph(layout: Layout, with_captions: bool) -> str:
    """Returns a filter_complex whose final video label is [v]."""
    if isinstance(layout, StackLayout):
        face_h = int(round(OUT_H * layout.facecam_ratio / 2)) * 2
        game_h = OUT_H - face_h
        graph = (
            "[0:v]split=2[a][b];"
            f"[a]{_crop(layout.facecam)},{_fill(OUT_W, face_h)}[face];"
            f"[b]{_crop(layout.gameplay)},{_fill(OUT_W, game_h)}[game];"
            "[face][game]vstack=inputs=2,setsar=1[base]"
        )
    elif isinstance(layout, BlurLayout):
        fg_w = int(round(OUT_W * layout.zoom / 2)) * 2
        graph = (
            "[0:v]split=2[a][b];"
            # Downscale before blurring: much cheaper and visually identical.
            f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
            f"gblur=sigma=12,scale={OUT_W}:{OUT_H},eq=brightness=-0.08[bg];"
            f"[b]scale={fg_w}:-2,crop=min(iw\\,{OUT_W}):ih[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]"
        )
    else:  # pragma: no cover - exhaustive over the Layout union
        raise MediaError(f"unsupported layout: {layout!r}")

    if with_captions:
        graph += f";[base]ass={CAPTIONS_FILENAME}[v]"
    else:
        graph += ";[base]null[v]"
    return graph


def _require_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe is None:
        raise MediaError("ffmpeg not found in PATH")
    return exe


def render_vertical(
    source: Path,
    output: Path,
    layout: Layout,
    captions_ass: str | None,
    work_dir: Path,
) -> Path:
    """Renders `source` into a 9:16 mp4 at `output`.

    ffmpeg runs with cwd=work_dir so the subtitle file is referenced by a plain relative
    name, sidestepping filtergraph escaping of absolute paths.
    """
    ffmpeg = _require_ffmpeg()
    work_dir.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    if captions_ass:
        (work_dir / CAPTIONS_FILENAME).write_text(captions_ass, encoding="utf-8")

    tmp_out = output.with_suffix(".tmp.mp4")
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source.resolve()),
        "-filter_complex", build_filtergraph(layout, with_captions=bool(captions_ass)),
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-fps_mode", "cfr",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-movflags", "+faststart",
        str(tmp_out.resolve()),
    ]
    log.debug("ffmpeg: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, cwd=work_dir, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired as exc:
        tmp_out.unlink(missing_ok=True)
        raise MediaError(f"ffmpeg timed out after {FFMPEG_TIMEOUT_S}s") from exc
    if proc.returncode != 0:
        tmp_out.unlink(missing_ok=True)
        raise MediaError(f"ffmpeg failed ({proc.returncode}): {proc.stderr.strip()[-800:]}")
    tmp_out.replace(output)  # atomic: never leave a half-written file at the final path
    return output


def extract_frame(source: Path, output: Path, at_s: float = 1.0) -> Path:
    """Saves a PNG frame — used to calibrate facecam/gameplay rects."""
    ffmpeg = _require_ffmpeg()
    output.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(at_s),
         "-i", str(source), "-frames:v", "1", str(output)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if proc.returncode != 0:
        raise MediaError(f"frame extraction failed: {proc.stderr.strip()[-400:]}")
    return output
