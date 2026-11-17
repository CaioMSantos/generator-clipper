"""Orchestration: discover -> rank -> persist -> render."""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from clipper.config import BlurLayout, Layout, Settings, StreamerCatalog
from clipper.media import MediaError
from clipper.media.captions import build_ass
from clipper.media.download import download_clip
from clipper.media.render import render_vertical
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


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        catalog: StreamerCatalog,
        store: Store,
        sources: list[ClipSource],
        transcriber: Transcriber,
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.store = store
        self.sources = sources
        self.transcriber = transcriber

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

        ranked = rank(clips, now, min_views=self.settings.min_views)
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
        rows = self.store.claim_next(self.settings.max_renders_per_cycle, created_after)
        done = 0
        for row in rows:
            try:
                result = self.render_clip(row.clip, score=row.score)
                self.store.set_status(row.key, ClipStatus.READY, output_path=str(result.output))
                done += 1
                log.info("READY %s (score=%.1f) -> %s", row.key, row.score, result.output)
            except MediaError as exc:
                log.error("FAILED %s (attempt %d): %s", row.key, row.attempts, exc)
                self.store.set_status(row.key, ClipStatus.FAILED, error=str(exc)[:1000])
        return done

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
    ) -> RenderResult:
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

        day = clip.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        out_dir = s.output_dir / day
        output = out_dir / f"{_safe(clip.broadcaster_login)}_{basename}.mp4"
        work_dir = s.data_dir / "work" / basename
        try:
            render_vertical(
                source,
                output,
                layout or self.layout_for(clip),
                build_ass(words) if words else None,
                work_dir,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        metadata = output.with_suffix(".json")
        metadata.write_text(
            json.dumps(_metadata(clip, score, words), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return RenderResult(output=output, metadata=metadata, words=len(words))

    def run_cycle(self) -> tuple[DiscoverResult, int]:
        discovered = self.discover()
        rendered = self.process_queue()
        return discovered, rendered


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)[:80]


def _metadata(clip: Clip, score: float, words: list[Word]) -> dict[str, object]:
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
        "suggested_title": clip.title,
        "suggested_caption": f"{clip.title}\n\nCréditos: {channel}\n\n{' '.join(tags)}",
        "transcript": " ".join(w.text for w in words),
    }
