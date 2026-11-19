"""ffmpeg reframing to 1080x1920 with burned captions and the hook title card."""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from clipper.config import BlurLayout, Layout, Rect, StackLayout
from clipper.media import MediaError
from clipper.media.fonts import FontSpec

log = logging.getLogger(__name__)

OUT_W = 1080
OUT_H = 1920
FFMPEG_TIMEOUT_S = 600
CAPTIONS_FILENAME = "captions.ass"
HOOK_FILENAME = "hook.png"
FONTS_SUBDIR = "fonts"
HOOK_DURATION_S = 4.0
HOOK_FADE_S = 0.4


@dataclass(frozen=True, slots=True)
class HookOverlay:
    """Hook card already rendered to `work_dir/hook.png`; `top_y` is its top edge in the output."""

    top_y: int
    duration_s: float = HOOK_DURATION_S


def hook_center_y(layout: Layout) -> int:
    """Where the hook card is centered: on the seam between facecam and gameplay.

    For blur, that is the top edge of the (assumed 16:9) foreground video.
    """
    if isinstance(layout, StackLayout):
        return int(round(OUT_H * layout.facecam_ratio / 2)) * 2
    fg_h = int(OUT_W * layout.zoom * 9 / 16)
    return max((OUT_H - fg_h) // 2, 0)


def _crop(r: Rect) -> str:
    # Even dimensions keep libx264/yuv420p happy.
    return (
        f"crop=trunc(iw*{r.w}/2)*2:trunc(ih*{r.h}/2)*2:"
        f"trunc(iw*{r.x}/2)*2:trunc(ih*{r.y}/2)*2"
    )


def _fill(w: int, h: int) -> str:
    """Scale to cover WxH, then center-crop to exactly WxH."""
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"


def build_filtergraph(
    layout: Layout,
    with_captions: bool,
    hook: HookOverlay | None = None,
    with_fontsdir: bool = False,
) -> str:
    """Returns a filter_complex whose final video label is [v].

    Input 0 is the clip; when `hook` is set, input 1 is the looped hook PNG.
    """
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

    last = "base"
    if with_captions:
        fontsdir = f":fontsdir={FONTS_SUBDIR}" if with_fontsdir else ""
        graph += f";[{last}]ass={CAPTIONS_FILENAME}{fontsdir}[capt]"
        last = "capt"
    if hook is not None:
        fade_start = max(hook.duration_s - HOOK_FADE_S, 0.0)
        graph += (
            f";[1:v]format=rgba,fade=t=out:st={fade_start:.2f}:d={HOOK_FADE_S:.2f}:alpha=1[hk]"
            f";[{last}][hk]overlay=x=(W-w)/2:y={hook.top_y}:eof_action=pass:format=auto[hooked]"
        )
        last = "hooked"
    graph += f";[{last}]null[v]"
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
    *,
    hook: HookOverlay | None = None,
    font: FontSpec | None = None,
    trim: tuple[float, float] | None = None,
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
    if font is not None:
        # libass only sees fonts in fontsdir (or the system); copy so the path needs no escaping.
        fonts = work_dir / FONTS_SUBDIR
        fonts.mkdir(exist_ok=True)
        shutil.copyfile(font.path, fonts / font.path.name)
    if hook is not None and not (work_dir / HOOK_FILENAME).is_file():
        raise MediaError(f"hook overlay requested but {HOOK_FILENAME} is missing in {work_dir}")
    hook_input = (
        ["-loop", "1", "-framerate", "60", "-t", f"{hook.duration_s:.2f}", "-i", HOOK_FILENAME]
        if hook is not None
        else []
    )

    trim_args: list[str] = []
    if trim is not None:
        start, end = trim
        if start < 0 or end <= start:
            raise MediaError(f"invalid trim {trim!r}")
        # Input seeking: fast and frame-accurate with re-encoding; output timestamps start at 0.
        trim_args = ["-ss", f"{start:.3f}", "-to", f"{end:.3f}"]

    tmp_out = output.with_suffix(".tmp.mp4")
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        *trim_args,
        "-i", str(source.resolve()),
        *hook_input,
        "-filter_complex", build_filtergraph(
            layout, with_captions=bool(captions_ass), hook=hook, with_fontsdir=font is not None
        ),
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
