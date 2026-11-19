"""Orchestration: discover -> rank -> persist -> render."""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from clipper.config import BlurLayout, Layout, Settings, StackLayout, StreamerCatalog
from clipper.media import MediaError
from clipper.hook import HookWriter
from clipper.media.captions import CaptionStyle, build_ass
from clipper.media.fonts import FontSpec
from clipper.media.hook_card import render_hook_card
from clipper.media.download import download_clip
from clipper.media.facecam import FaceDetector, pick_facecam
from clipper.media.render import HOOK_FILENAME, HookOverlay, hook_center_y, render_vertical
from clipper.media.transcribe import Transcriber, Word
from clipper.models import Clip, ClipStatus
from clipper.ranking import rank
from clipper.sources import ClipSource, SourceError
from clipper.store import Store

log = logging.getLogger(__name__)

# Statuses that mean "this moment was already taken care of" — don't render it again.
HANDLED = {
    ClipStatus.RENDERING, ClipStatus.READY, ClipStatus.APPROVED,
    ClipStatus.REJECTED, ClipStatus.POSTED, ClipStatus.FAILED,
}


@dataclass(frozen=True, slots=True)
class DiscoverResult:
    fetched: int
    moments: int
    queued: int


@dataclass(frozen=True, slots=True)
class RenderResult:
    output: Path
    metadata: Path
    words: int
    hook: str | None = None


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        catalog: StreamerCatalog,
        store: Store,
        sources: list[ClipSource],
        transcriber: Transcriber,
        hook_writer: HookWriter | None = None,
        font: FontSpec | None = None,
        face_detector: FaceDetector | None = None,
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.store = store
        self.sources = sources
        self.transcriber = transcriber
        self.hook_writer = hook_writer
        self.font = font
        self.face_detector = face_detector

    # ---- discovery ----------------------------------------------------------

    def discover(self, now: datetime | None = None) -> DiscoverResult:
        now = now or datetime.now(timezone.utc)
        since = now - timedelta(hours=self.settings.lookback_hours)
        streamers = self.catalog.enabled()

        clips: list[Clip] = []
        for source in self.sources:
            try:
                clips.extend(source.fetch_clips(streamers, since))
            except SourceError as exc:
                log.error("source %s failed: %s", type(source).__name__, exc)

        too_long = [c for c in clips if self._too_long(c)]
        if too_long:
            log.info("discover: dropped %d clips longer than %.0fs", len(too_long), self.settings.max_clip_seconds)
            clips = [c for c in clips if not self._too_long(c)]

        ranked = rank(clips, now, min_views=self.min_views_for)
        queued = 0
        for r in ranked:
            cluster = [r.clip, *r.duplicates]
            statuses = self.store.statuses([c.key for c in cluster])
            if any(s in HANDLED for s in statuses.values()):
                for c in cluster:
                    self.store.mark_skipped(c)
                continue
            self.store.upsert_discovered(r.clip, r.score, r.cluster_size)
            for d in r.duplicates:
                self.store.mark_skipped(d)
            queued += 1
        log.info("discover: %d clips -> %d moments -> %d queued", len(clips), len(ranked), queued)
        return DiscoverResult(fetched=len(clips), moments=len(ranked), queued=queued)

    # ---- rendering ----------------------------------------------------------

    def process_queue(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        created_after = now - timedelta(hours=self.settings.lookback_hours)
        rows = self.store.claim_next(
            self.settings.max_renders_per_cycle,
            created_after,
            per_streamer=self.settings.max_renders_per_streamer or None,
        )
        done = 0
        for row in rows:
            if self._too_long(row.clip):  # queued before the limit existed or was lowered
                log.info("SKIPPED %s: %.0fs is over the duration limit", row.key, row.clip.duration_s)
                self.store.set_status(row.key, ClipStatus.SKIPPED, error="too long")
                continue
            try:
                result = self.render_clip(row.clip, score=row.score)
                self.store.set_status(row.key, ClipStatus.READY, output_path=str(result.output))
                done += 1
                log.info("READY %s (score=%.1f) -> %s", row.key, row.score, result.output)
            except MediaError as exc:
                log.error("FAILED %s (attempt %d): %s", row.key, row.attempts, exc)
                self.store.set_status(row.key, ClipStatus.FAILED, error=str(exc)[:1000])
        return done

    def _too_long(self, clip: Clip) -> bool:
        limit = self.settings.max_clip_seconds
        return limit > 0 and clip.duration_s > limit

    def min_views_for(self, clip: Clip) -> int:
        streamer = self.catalog.find(clip.platform, clip.broadcaster_login)
        if streamer is not None and streamer.min_views is not None:
            return streamer.min_views
        return self.settings.min_views

    def layout_for(self, clip: Clip) -> Layout:
        streamer = self.catalog.find(clip.platform, clip.broadcaster_login)
        return streamer.layout if streamer else BlurLayout()

    def render_clip(
        self,
        clip: Clip,
        *,
        score: float = 0.0,
        layout: Layout | None = None,
        captions: bool = True,
        hook_text: str | None = None,
        trim: tuple[float, float] | None = None,
    ) -> RenderResult:
        """Downloads, transcribes, writes the hook and renders.

        `hook_text`: None asks the AI, "" renders without a hook, anything else is used as is.
        `trim`: (start, end) in seconds of the source clip.
        """
        s = self.settings
        basename = f"{clip.platform.value}_{_safe(clip.id)}"
        source = download_clip(clip.url, s.downloads_dir, basename)

        words: list[Word] = []
        if captions:
            try:
                words = self.transcriber.transcribe(source)
            except MediaError as exc:
                # A clip without captions is still worth reviewing; don't lose the moment.
                log.warning("captions skipped for %s: %s", clip.key, exc)

        if trim is not None:
            words = shift_words(words, *trim)

        transcript = " ".join(w.text for w in words)
        hook: str | None
        if hook_text is not None:
            hook = hook_text.strip().upper() or None
        elif self.hook_writer is not None:
            hook = self.hook_writer.write(clip, transcript)  # never raises; None on failure
        else:
            hook = None

        day = clip.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        out_dir = s.output_dir / day
        output = out_dir / f"{_safe(clip.broadcaster_login)}_{basename}.mp4"
        work_dir = s.data_dir / "work" / basename
        chosen_layout = self._resolve_layout(source, layout or self.layout_for(clip))
        style = CaptionStyle(font=self.font.family) if self.font else CaptionStyle()
        try:
            overlay = self._prepare_hook(hook, chosen_layout, work_dir) if hook else None
            render_vertical(
                source,
                output,
                chosen_layout,
                build_ass(words, style) if words else None,
                work_dir,
                hook=overlay,
                font=self.font,
                trim=trim,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        metadata = output.with_suffix(".json")
        metadata.write_text(
            json.dumps(_metadata(clip, score, words, hook, trim), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return RenderResult(output=output, metadata=metadata, words=len(words), hook=hook)

    def _resolve_layout(self, source: Path, layout: Layout) -> Layout:
        """Picks this clip's facecam among the candidates; no face found -> blur."""
        if not isinstance(layout, StackLayout) or not self.settings.facecam_detect:
            return layout
        candidates = [layout.facecam, *layout.facecam_alternates]
        try:
            found = pick_facecam(source, candidates, self.face_detector)
        except Exception as exc:  # detection is best effort; never lose the clip over it
            log.warning("facecam detection failed for %s, using calibrated rect: %s", source.name, exc)
            return layout
        if found is None:
            log.info("facecam not found in %s (scene without camera?), using blur layout", source.name)
            return BlurLayout()
        return layout.model_copy(update={"facecam": found})

    def _prepare_hook(self, hook: str, layout: Layout, work_dir: Path) -> HookOverlay | None:
        if self.font is None:
            log.warning("hook skipped: no font available")
            return None
        try:
            png = render_hook_card(hook, self.font.path, work_dir / HOOK_FILENAME)
        except MediaError as exc:
            log.warning("hook card failed, rendering without hook: %s", exc)
            return None
        from PIL import Image  # local: only needed here

        with Image.open(png) as img:
            height = img.height
        return HookOverlay(top_y=max(hook_center_y(layout) - height // 2, 0))

    def run_cycle(self) -> tuple[DiscoverResult, int]:
        discovered = self.discover()
        rendered = self.process_queue()
        return discovered, rendered


def shift_words(words: list[Word], start: float, end: float) -> list[Word]:
    """Keeps the words inside [start, end] and rebases their timestamps to the trimmed clip."""
    out: list[Word] = []
    for w in words:
        if w.end <= start or w.start >= end:
            continue
        out.append(Word(text=w.text, start=max(w.start, start) - start, end=min(w.end, end) - start))
    return out


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)[:80]


def _metadata(
    clip: Clip,
    score: float,
    words: list[Word],
    hook: str | None = None,
    trim: tuple[float, float] | None = None,
) -> dict[str, object]:
    channel = (
        f"twitch.tv/{clip.broadcaster_login}"
        if clip.platform.value == "twitch"
        else f"kick.com/{clip.broadcaster_login}"
    )
    tags = ["#gta6", "#gtavi", f"#{clip.broadcaster_login}", "#cortes", "#gta"]
    return {
        "clip_id": clip.id,
        "platform": clip.platform.value,
        "broadcaster": clip.broadcaster_name,
        "source_url": clip.url,
        "views_at_render": clip.view_count,
        "score": score,
        "created_at": clip.created_at.isoformat(),
        "hook": hook,
        "trim": list(trim) if trim else None,
        "suggested_title": hook or clip.title,
        "suggested_caption": "\n\n".join(
            part for part in ((hook or clip.title).strip(), f"Créditos: {channel}", " ".join(tags)) if part
        ),
        "transcript": " ".join(w.text for w in words),
    }
