"""FastAPI app for the review panel.

Reads the same SQLite queue the `run` loop writes (WAL allows concurrent readers). Each request
opens its own Store so connections never cross threads.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from clipper.config import Settings, StreamerCatalog
from clipper.models import ClipStatus
from clipper.panel.jobs import Job, RerenderQueue, RerenderRequest
from clipper.pipeline import Pipeline
from clipper.store import ClipRow, Store

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
# What the panel shows: everything past discovery that a human may want to look at.
REVIEW_STATUSES = [
    ClipStatus.READY, ClipStatus.APPROVED, ClipStatus.REJECTED,
    ClipStatus.POSTED, ClipStatus.RENDERING, ClipStatus.FAILED,
]  # fmt: skip
SETTABLE = {ClipStatus.READY, ClipStatus.APPROVED, ClipStatus.REJECTED, ClipStatus.POSTED}


# ---- API models -------------------------------------------------------------


class ClipView(BaseModel):
    key: str
    id: str
    platform: str
    streamer: str
    streamer_name: str
    title: str
    source_url: str
    views: int
    score: float
    cluster_size: int
    status: ClipStatus
    created_at: datetime
    updated_at: datetime | None
    duration_s: float
    error: str | None
    has_video: bool
    video_url: str | None
    hook: str | None = None
    suggested_caption: str | None = None
    transcript: str | None = None
    trim_start: float = 0.0  # where the rendered video starts inside the source clip


class StreamerStat(BaseModel):
    platform: str
    login: str
    name: str
    to_review: int


class Stats(BaseModel):
    by_status: dict[str, int]
    streamers: list[StreamerStat]
    last_activity: datetime | None
    output_dir: str


class StatusUpdate(BaseModel):
    status: ClipStatus


class RerenderBody(BaseModel):
    hook: str | None = Field(default=None, description='None = AI writes it, "" = no hook')
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, gt=0)
    captions: bool = True

    @model_validator(mode="after")
    def _trim_pair(self) -> RerenderBody:
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must be sent together")
        if self.start is not None and self.end is not None and self.end - self.start < 1:
            raise ValueError("the trimmed clip must be at least 1s long")
        return self


class JobView(BaseModel):
    id: int
    key: str
    state: Literal["queued", "running", "done", "failed"]
    error: str | None
    created_at: datetime
    finished_at: datetime | None


# ---- helpers ----------------------------------------------------------------


def _read_metadata(video: Path) -> dict[str, Any]:
    meta = video.with_suffix(".json")
    if not meta.is_file():
        return {}
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("bad metadata %s: %s", meta, exc)
        return {}


def _trim_start(meta: dict[str, Any]) -> float:
    trim = meta.get("trim")
    if isinstance(trim, list) and trim and isinstance(trim[0], (int, float)):
        return float(trim[0])
    return 0.0


def to_view(row: ClipRow) -> ClipView:
    video = Path(row.output_path) if row.output_path else None
    has_video = bool(video and video.is_file())
    meta = _read_metadata(video) if video and has_video else {}
    version = int(video.stat().st_mtime) if video and has_video else 0
    c = row.clip
    return ClipView(
        key=row.key, id=c.id, platform=c.platform.value, streamer=c.broadcaster_login,
        streamer_name=c.broadcaster_name, title=c.title, source_url=c.url, views=c.view_count,
        score=row.score, cluster_size=row.cluster_size, status=row.status, created_at=c.created_at,
        updated_at=row.updated_at, duration_s=c.duration_s, error=row.error, has_video=has_video,
        video_url=f"/media/{row.key}?v={version}" if has_video else None,
        hook=meta.get("hook"), suggested_caption=meta.get("suggested_caption"),
        transcript=meta.get("transcript"), trim_start=_trim_start(meta),
    )  # fmt: skip


def _job_view(job: Job) -> JobView:
    return JobView(
        id=job.id, key=job.key, state=job.state.value, error=job.error,
        created_at=job.created_at, finished_at=job.finished_at,
    )  # fmt: skip


# ---- app --------------------------------------------------------------------


def create_app(
    settings: Settings,
    catalog: StreamerCatalog,
    pipeline_factory: Callable[[], Pipeline],
) -> FastAPI:
    queue = RerenderQueue(pipeline_factory)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Any:
        yield
        queue.shutdown()

    app = FastAPI(title="gta6-clipper panel", lifespan=lifespan)

    def get_store() -> Iterator[Store]:
        store = Store(settings.db_path)
        try:
            yield store
        finally:
            store.close()

    @app.get("/api/clips", response_model=list[ClipView])
    def list_clips(
        status: ClipStatus | None = None,
        streamer: str | None = None,
        q: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
        store: Store = Depends(get_store),
    ) -> list[ClipView]:
        statuses = [status] if status else REVIEW_STATUSES
        views = [to_view(r) for r in store.list_by_statuses(statuses, streamer, limit)]
        if q:
            needle = q.lower()
            views = [
                v for v in views
                if needle in " ".join(filter(None, [v.title, v.hook, v.transcript, v.streamer_name])).lower()
            ]  # fmt: skip
        return views

    @app.get("/api/stats", response_model=Stats)
    def stats(store: Store = Depends(get_store)) -> Stats:
        to_review = store.count_by_streamer([ClipStatus.READY])
        streamers = [
            StreamerStat(
                platform=s.platform.value, login=s.login.lower(), name=s.name,
                to_review=to_review.get((s.platform.value, s.login.lower()), 0),
            )
            for s in catalog.enabled()
        ]  # fmt: skip
        return Stats(
            by_status={k.value: v for k, v in store.count_by_status().items()},
            streamers=streamers,
            last_activity=store.last_activity(),
            output_dir=str(settings.output_dir.resolve()),
        )

    @app.post("/api/clips/{key}/status", response_model=ClipView)
    def set_status(key: str, body: StatusUpdate, store: Store = Depends(get_store)) -> ClipView:
        if body.status not in SETTABLE:
            raise HTTPException(422, f"status must be one of {sorted(s.value for s in SETTABLE)}")
        row = store.get(key)
        if row is None:
            raise HTTPException(404, "clip not found")
        if row.status == ClipStatus.RENDERING:
            raise HTTPException(409, "clip is rendering")
        store.set_status(key, body.status, error=row.error)
        updated = store.get(key)
        assert updated is not None
        return to_view(updated)

    @app.post("/api/clips/{key}/rerender", response_model=JobView, status_code=202)
    def rerender(key: str, body: RerenderBody, store: Store = Depends(get_store)) -> JobView:
        row = store.get(key)
        if row is None:
            raise HTTPException(404, "clip not found")
        if body.end is not None and row.clip.duration_s and body.end > row.clip.duration_s + 0.5:
            raise HTTPException(422, f"end is past the clip duration ({row.clip.duration_s:.0f}s)")
        trim = (body.start, body.end) if body.start is not None and body.end is not None else None
        job = queue.submit(key, RerenderRequest(hook_text=body.hook, trim=trim, captions=body.captions))
        return _job_view(job)

    @app.get("/api/jobs", response_model=list[JobView])
    def jobs() -> list[JobView]:
        return [_job_view(j) for j in queue.jobs()]

    @app.get("/media/{key}")
    def media(key: str, download: bool = False, store: Store = Depends(get_store)) -> FileResponse:
        row = store.get(key)
        if row is None or not row.output_path or not Path(row.output_path).is_file():
            raise HTTPException(404, "video not found")
        # FileResponse handles HTTP Range, so the player can seek.
        return FileResponse(
            row.output_path, media_type="video/mp4", filename=Path(row.output_path).name,
            content_disposition_type="attachment" if download else "inline",
        )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
