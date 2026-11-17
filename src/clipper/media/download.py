"""Clip download through yt-dlp (supports Twitch and Kick clip URLs)."""

from __future__ import annotations

import logging
from pathlib import Path

import yt_dlp

from clipper.media import MediaError

log = logging.getLogger(__name__)


def download_clip(url: str, dest_dir: Path, basename: str) -> Path:
    """Downloads `url` to `dest_dir/basename.<ext>` and returns the file path.

    Reuses an existing download (idempotent retries).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(dest_dir.glob(f"{basename}.*"))
    existing = [p for p in existing if p.suffix not in {".part", ".ytdl"}]
    if existing:
        return existing[0]

    opts = {
        "outtmpl": str(dest_dir / f"{basename}.%(ext)s"),
        # Prefer the best single-file mp4; clips are short so bandwidth is not a concern.
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "socket_timeout": 20,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadError as exc:
        raise MediaError(f"download failed for {url}: {exc}") from exc

    if not path.is_file() or path.stat().st_size == 0:
        raise MediaError(f"download produced no file for {url}")
    log.info("downloaded %s -> %s (%.1f MB)", url, path.name, path.stat().st_size / 1e6)
    return path
