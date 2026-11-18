"""Command line entrypoint: `clipper <command>`."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from types import FrameType

from clipper.config import ConfigError, Platform, Settings, load_catalog
from clipper.media import MediaError
from clipper.hook import ClaudeHookWriter
from clipper.media.download import download_clip
from clipper.media.fonts import FontSpec, resolve_font
from clipper.media.render import extract_frame
from clipper.media.transcribe import WhisperTranscriber
from clipper.models import Clip, ClipStatus
from clipper.pipeline import Pipeline
from clipper.sources import ClipSource, SourceError
from clipper.sources.kick import KickSource
from clipper.sources.twitch import TwitchSource
from clipper.store import Store

log = logging.getLogger("clipper")
_stop = False


def _handle_stop(signum: int, _frame: FrameType | None) -> None:
    global _stop
    _stop = True
    log.info("signal %d received, finishing current step...", signum)


def _build(settings: Settings, need_sources: bool = True) -> Pipeline:
    catalog = load_catalog(settings.streamers_file)
    sources: list[ClipSource] = []
    if need_sources:
        if catalog.enabled(Platform.TWITCH):
            sources.append(
                TwitchSource(
                    settings.twitch_client_id,
                    settings.twitch_client_secret,
                    game_names=settings.game_name_list,
                )
            )
        if catalog.enabled(Platform.KICK):
            sources.append(KickSource(game_names=settings.game_name_list))
    store = Store(settings.db_path)
    transcriber = WhisperTranscriber(settings.whisper_model, settings.whisper_language)
    try:
        spec = resolve_font(settings.fonts_dir)
        log.info("font: %s (%s)", spec.family, spec.path)
        font: FontSpec | None = spec
    except MediaError as exc:
        log.warning("%s; captions use libass default font and hooks are disabled", exc)
        font = None
    hook_writer = (
        ClaudeHookWriter(settings.hook_model, settings.anthropic_api_key or None)
        if settings.hook_enabled
        else None
    )
    return Pipeline(settings, catalog, store, sources, transcriber, hook_writer, font)


def _platform_from_url(url: str) -> Platform:
    return Platform.KICK if "kick.com" in url else Platform.TWITCH


def cmd_run(settings: Settings, once: bool) -> int:
    pipe = _build(settings)
    recovered = pipe.store.recover_stuck()
    if recovered:
        log.info("recovered %d clips stuck in 'rendering'", recovered)
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)
    while not _stop:
        started = time.monotonic()
        try:
            d, r = pipe.run_cycle()
            log.info("cycle: fetched=%d moments=%d queued=%d rendered=%d", d.fetched, d.moments, d.queued, r)
        except Exception:  # keep the daemon alive; the next cycle may succeed
            log.exception("cycle crashed")
        if once:
            break
        remaining = settings.poll_interval_seconds - (time.monotonic() - started)
        while remaining > 0 and not _stop:
            time.sleep(min(remaining, 1.0))
            remaining -= 1.0
    return 0


def _lookup_twitch_clip(settings: Settings, clip_id: str, login: str | None) -> Clip | None:
    """Best effort: real title/views/created_at from Helix. Falls back to a placeholder."""
    if not (settings.twitch_client_id and settings.twitch_client_secret):
        return None
    try:
        return TwitchSource(settings.twitch_client_id, settings.twitch_client_secret).fetch_clip(
            clip_id, login
        )
    except SourceError as exc:
        log.warning("could not fetch clip metadata from twitch (%s); using placeholder", exc)
        return None


def cmd_render(
    settings: Settings, url: str, streamer: str | None, captions: bool, hook: str | None = None
) -> int:
    """Renders an arbitrary clip URL (manual mode / testing layouts)."""
    pipe = _build(settings, need_sources=False)
    platform = _platform_from_url(url)
    clip_id = _clip_id_from_url(url)
    login = streamer.lower() if streamer else None
    clip = _lookup_twitch_clip(settings, clip_id, login) if platform == Platform.TWITCH else None
    if clip is None:
        clip = Clip(
            id=clip_id,
            platform=platform,
            broadcaster_login=login or "manual",
            broadcaster_name=streamer or "manual",
            title="",
            url=url,
            view_count=0,
            created_at=datetime.now(timezone.utc),
            duration_s=0,
        )
    result = pipe.render_clip(clip, captions=captions, hook_text=hook)
    print(f"{result.output}\n{result.metadata}\nwords={result.words}\nhook={result.hook!r}")
    return 0


def cmd_frame(settings: Settings, url: str, at: float) -> int:
    basename = f"frame_{_clip_id_from_url(url)}"
    src = download_clip(url, settings.downloads_dir, basename)
    out = extract_frame(src, settings.data_dir / "frames" / f"{basename}.png", at)
    print(out)
    return 0


def cmd_list(settings: Settings, status: str | None, limit: int) -> int:
    store = Store(settings.db_path)
    rows = store.list_clips(ClipStatus(status) if status else None, limit)
    for r in rows:
        print(
            f"{r.status.value:<10} score={r.score:>8.1f} views={r.clip.view_count:>6} "
            f"x{r.cluster_size} {r.clip.broadcaster_login:<12} {r.clip.title[:50]!r} "
            f"{r.output_path or r.error or r.clip.url}"
        )
    return 0


def cmd_set_status(settings: Settings, key: str, status: str) -> int:
    Store(settings.db_path).set_status(key, ClipStatus(status))
    return 0


def _clip_id_from_url(url: str) -> str:
    tail = url.rstrip("/").split("?clip=")[-1].split("/")[-1].split("?")[0]
    return tail or str(int(time.time()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clipper", description="Cortes automáticos 9:16")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="loop: descobre clips e renderiza os melhores")
    p_run.add_argument("--once", action="store_true", help="roda um ciclo e sai")

    p_render = sub.add_parser("render", help="renderiza um clip específico pela URL")
    p_render.add_argument("url")
    p_render.add_argument("--streamer", help="login do streamer (usa o layout configurado)")
    p_render.add_argument("--no-captions", action="store_true")
    p_render.add_argument("--hook", help="texto do gancho (pula a geração por IA)")

    p_frame = sub.add_parser("frame", help="salva um PNG do clip para calibrar o layout")
    p_frame.add_argument("url")
    p_frame.add_argument("--at", type=float, default=1.0)

    p_list = sub.add_parser("list", help="lista a fila")
    p_list.add_argument("--status", choices=[s.value for s in ClipStatus])
    p_list.add_argument("--limit", type=int, default=30)

    p_set = sub.add_parser("set-status", help="marca um clip (ex.: approved/rejected/posted)")
    p_set.add_argument("key", help="ex.: twitch:AbcClipId")
    p_set.add_argument("status", choices=[s.value for s in ClipStatus])

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in (
        "httpx", "httpcore", "httpx2", "httpcore2", "faster_whisper", "huggingface_hub", "filelock",
        "anthropic", "PIL",
    ):  # fmt: skip
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        settings = Settings()
        if args.cmd == "run":
            return cmd_run(settings, args.once)
        if args.cmd == "render":
            return cmd_render(settings, args.url, args.streamer, not args.no_captions, args.hook)
        if args.cmd == "frame":
            return cmd_frame(settings, args.url, args.at)
        if args.cmd == "list":
            return cmd_list(settings, args.status, args.limit)
        if args.cmd == "set-status":
            return cmd_set_status(settings, args.key, args.status)
    except (ConfigError, SourceError, MediaError, KeyError) as exc:
        log.error("%s", exc)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
