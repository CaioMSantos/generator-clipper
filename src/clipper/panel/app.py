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

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from clipper.config import (
    BlurLayout, ConfigError, Layout, Platform, Rect, Settings, StackLayout, StreamerCatalog, load_catalog,
)
from clipper.media import MediaError
from clipper.media.download import download_clip
from clipper.media.facecam import score_candidates
from clipper.media.render import extract_frame
from clipper.models import ClipStatus
from clipper.panel.connections import (
    CONNECTIONS, SPECS, CatalogEditor, ConnectionId, ConnectionTester, EnvStore, mask,
)
from clipper.panel.jobs import Job, RerenderQueue, RerenderRequest
from clipper.sources import SourceError
from clipper.pipeline import Pipeline, safe_name
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


class FieldView(BaseModel):
    key: str
    label: str
    secret: bool
    is_set: bool
    preview: str | None  # masked for secrets, full value otherwise


class TestView(BaseModel):
    ok: bool
    message: str
    latency_ms: int
    at: float


class ConnectionView(BaseModel):
    id: str
    name: str
    description: str
    docs_url: str
    required: bool
    configured: bool
    fields: list[FieldView]
    last_test: TestView | None


class ConnectionUpdate(BaseModel):
    # value: string sets it, null clears it, missing keys are untouched
    values: dict[str, str | None]


class StreamerView(BaseModel):
    name: str
    platform: str
    login: str
    enabled: bool
    min_views: int | None
    layout: str
    facecam_positions: int


class StreamerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    platform: Platform
    login: str = Field(min_length=2, max_length=40)
    min_views: int | None = Field(default=None, ge=0)
    verify: bool = True


class StreamerPatch(BaseModel):
    enabled: bool | None = None
    name: str | None = Field(default=None, max_length=60)
    min_views: int | None = Field(default=None, ge=0)
    clear_min_views: bool = False


class LayoutBody(BaseModel):
    mode: Literal["stack", "blur"]
    facecam: Rect | None = None
    facecam_alternates: list[Rect] = Field(default_factory=list, max_length=8)
    gameplay: Rect | None = None
    facecam_ratio: float = Field(default=0.30, ge=0.15, le=0.5)
    zoom: float = Field(default=1.0, ge=1.0, le=2.0)

    def to_layout(self) -> Layout:
        if self.mode == "blur":
            return BlurLayout(zoom=self.zoom)
        if self.facecam is None:
            raise ValueError("o modo stack precisa de pelo menos uma posição de câmera")
        return StackLayout(
            mode="stack", facecam=self.facecam, facecam_alternates=self.facecam_alternates,
            gameplay=self.gameplay or Rect(x=0.0, y=0.0, w=1.0, h=1.0), facecam_ratio=self.facecam_ratio,
        )  # fmt: skip

    @classmethod
    def from_layout(cls, layout: Layout) -> LayoutBody:
        if isinstance(layout, StackLayout):
            return cls(
                mode="stack", facecam=layout.facecam, facecam_alternates=layout.facecam_alternates,
                gameplay=layout.gameplay, facecam_ratio=layout.facecam_ratio,
            )  # fmt: skip
        return cls(mode="blur", zoom=layout.zoom)


class CalibClip(BaseModel):
    key: str
    title: str
    created_at: datetime
    duration_s: float
    downloaded: bool


class DetectBody(BaseModel):
    clip: str
    layout: LayoutBody


class DetectView(BaseModel):
    samples: int
    hits: list[int]
    chosen: int | None
    message: str


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


LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1", "testserver"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PANEL_HEADER = "x-clipper-panel"


def create_app(
    settings: Settings,
    catalog: StreamerCatalog,
    pipeline_factory: Callable[[], Pipeline],
    *,
    env_path: Path = Path(".env"),
    tester: ConnectionTester | None = None,
    allowed_hosts: set[str] | None = None,
) -> FastAPI:
    queue = RerenderQueue(pipeline_factory)
    env = EnvStore(env_path)
    editor = CatalogEditor(settings.streamers_file)
    tests = tester or ConnectionTester()
    hosts = LOCAL_HOSTS | {h.lower() for h in (allowed_hosts or set())}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Any:
        yield
        queue.shutdown()

    app = FastAPI(title="gta6-clipper panel", lifespan=lifespan)

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Any]) -> Response:
        """No login on the panel, so: only local Host headers (blocks DNS rebinding) and,
        for writes, same-origin plus a custom header (a cross-site page can't send it)."""
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
        if host not in hosts:
            return JSONResponse({"detail": f"host '{host}' not allowed"}, status_code=403)
        if request.method not in SAFE_METHODS and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1].rsplit(":", 1)[0].lower() not in hosts:
                return JSONResponse({"detail": "cross-origin write blocked"}, status_code=403)
            if request.headers.get(PANEL_HEADER) != "1":
                return JSONResponse({"detail": f"missing {PANEL_HEADER} header"}, status_code=403)
        response: Response = await call_next(request)
        return response

    def current_catalog() -> StreamerCatalog:
        try:
            return load_catalog(settings.streamers_file)
        except ConfigError as exc:
            log.warning("catalog reload failed, using the startup copy: %s", exc)
            return catalog

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
            for s in current_catalog().enabled()
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

    # ---- connections ------------------------------------------------------

    def connection_view(conn_id: str, values: dict[str, str]) -> ConnectionView:
        spec = SPECS[conn_id]
        fields = [
            FieldView(
                key=f.key, label=f.label, secret=f.secret, is_set=bool(values.get(f.key)),
                preview=(mask(values.get(f.key, "")) if f.secret else values.get(f.key) or None),
            )
            for f in spec.fields
        ]  # fmt: skip
        last = tests.last.get(conn_id)
        return ConnectionView(
            id=spec.id, name=spec.name, description=spec.description, docs_url=spec.docs_url,
            required=spec.required, configured=all(f.is_set for f in fields), fields=fields,
            last_test=TestView(ok=last.ok, message=last.message, latency_ms=last.latency_ms, at=last.at) if last else None,
        )  # fmt: skip

    @app.get("/api/connections", response_model=list[ConnectionView])
    def list_connections() -> list[ConnectionView]:
        values = env.read()
        return [connection_view(c.id, values) for c in CONNECTIONS]

    @app.put("/api/connections/{conn_id}", response_model=ConnectionView)
    def update_connection(conn_id: ConnectionId, body: ConnectionUpdate) -> ConnectionView:
        allowed = {f.key for f in SPECS[conn_id].fields}
        unknown = set(body.values) - allowed
        if unknown:
            raise HTTPException(422, f"campos desconhecidos para {conn_id}: {sorted(unknown)}")
        changes = {k: (v or "").strip() for k, v in body.values.items()}
        try:
            env.update(changes)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        tests.last.pop(conn_id, None)  # stale after a credential change
        log.info("connection %s updated: %s", conn_id, sorted(changes))
        return connection_view(conn_id, env.read())

    @app.post("/api/connections/{conn_id}/test", response_model=TestView)
    def test_connection(conn_id: ConnectionId) -> TestView:
        kick_logins = [s.login for s in current_catalog().streamers if s.platform == Platform.KICK]
        r = tests.run(conn_id, env.read(), kick_probe=kick_logins[0] if kick_logins else "kick")
        return TestView(ok=r.ok, message=r.message, latency_ms=r.latency_ms, at=r.at)

    # ---- streamers ---------------------------------------------------------

    def streamer_views() -> list[StreamerView]:
        return [
            StreamerView(
                name=s.name, platform=s.platform.value, login=s.login.lower(), enabled=s.enabled,
                min_views=s.min_views, layout=s.layout.mode,
                facecam_positions=(1 + len(s.layout.facecam_alternates)) if isinstance(s.layout, StackLayout) else 0,
            )
            for s in current_catalog().streamers
        ]  # fmt: skip

    @app.get("/api/streamers", response_model=list[StreamerView])
    def list_streamers() -> list[StreamerView]:
        return streamer_views()

    @app.post("/api/streamers", response_model=list[StreamerView], status_code=201)
    def add_streamer(body: StreamerCreate) -> list[StreamerView]:
        login = body.login.strip().lower()
        if body.verify:
            values = env.read()
            try:
                if body.platform == Platform.TWITCH:
                    exists = tests.twitch_factory(
                        values.get("TWITCH_CLIENT_ID", ""), values.get("TWITCH_CLIENT_SECRET", "")
                    ).channel_exists(login)
                else:
                    exists = tests.kick_factory().channel_exists(login)
            except SourceError as exc:
                raise HTTPException(424, f"não deu para verificar o canal: {exc}") from exc
            if not exists:
                raise HTTPException(404, f"canal '{login}' não encontrado na {body.platform.value}")
        try:
            editor.add(body.name, body.platform, login, body.min_views)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        log.info("streamer added: %s:%s", body.platform.value, login)
        return streamer_views()

    @app.patch("/api/streamers/{platform}/{login}", response_model=list[StreamerView])
    def patch_streamer(platform: Platform, login: str, body: StreamerPatch) -> list[StreamerView]:
        min_views: int | None | Literal["unset"] = "unset"
        if body.clear_min_views:
            min_views = None
        elif body.min_views is not None:
            min_views = body.min_views
        try:
            editor.update(platform, login, enabled=body.enabled, min_views=min_views, name=body.name)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return streamer_views()

    @app.delete("/api/streamers/{platform}/{login}", response_model=list[StreamerView])
    def delete_streamer(platform: Platform, login: str) -> list[StreamerView]:
        try:
            editor.remove(platform, login)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        log.info("streamer removed: %s:%s", platform.value, login)
        return streamer_views()

    # ---- calibration editor ----------------------------------------------

    def find_streamer(platform: Platform, login: str) -> Any:
        s = current_catalog().find(platform, login)
        if s is None:
            raise HTTPException(404, f"{platform.value}:{login} não está no catálogo")
        return s

    def downloaded_path(platform: Platform, clip_id: str) -> Path | None:
        base = f"{platform.value}_{safe_name(clip_id)}"
        hits = [p for p in settings.downloads_dir.glob(f"{base}.*") if p.suffix not in {".part", ".ytdl"}]
        return hits[0] if hits else None

    def source_for(store: Store, key: str, platform: Platform, login: str) -> Path:
        row = store.get(key)
        if row is None or row.clip.platform != platform or row.clip.broadcaster_login != login.lower():
            raise HTTPException(404, "clip não encontrado para este streamer")
        path = downloaded_path(platform, row.clip.id)
        if path is not None:
            return path
        try:  # not downloaded yet (e.g. skipped duplicate): fetch it now, ~5s
            return download_clip(row.clip.url, settings.downloads_dir, f"{platform.value}_{safe_name(row.clip.id)}")
        except MediaError as exc:
            raise HTTPException(502, f"não deu para baixar o clip: {exc}") from exc

    @app.get("/api/streamers/{platform}/{login}/clips", response_model=list[CalibClip])
    def calib_clips(platform: Platform, login: str, store: Store = Depends(get_store)) -> list[CalibClip]:
        find_streamer(platform, login)
        rows = store.recent_for_streamer(platform, login, limit=40)
        views = [
            CalibClip(
                key=r.key, title=r.clip.title or r.clip.id, created_at=r.clip.created_at,
                duration_s=r.clip.duration_s, downloaded=downloaded_path(platform, r.clip.id) is not None,
            )
            for r in rows
        ]  # fmt: skip
        views.sort(key=lambda v: (not v.downloaded, -v.created_at.timestamp()))  # instant ones first
        return views[:20]

    @app.get("/api/streamers/{platform}/{login}/frame")
    def calib_frame(
        platform: Platform, login: str, clip: str, at: float = Query(default=4.0, ge=0, le=600),
        store: Store = Depends(get_store),
    ) -> FileResponse:  # fmt: skip
        find_streamer(platform, login)
        source = source_for(store, clip, platform, login)
        out = settings.data_dir / "frames" / f"calib_{safe_name(clip)}_{at:.1f}.png"
        if not out.is_file():
            try:
                extract_frame(source, out, at)
            except MediaError as exc:
                raise HTTPException(422, f"não deu para extrair o frame em {at:.1f}s: {exc}") from exc
            if not out.is_file():  # ffmpeg writes nothing when `at` is past the end
                raise HTTPException(422, f"o clip é mais curto que {at:.1f}s")
        return FileResponse(out, media_type="image/png", headers={"Cache-Control": "max-age=3600"})

    @app.get("/api/streamers/{platform}/{login}/layout", response_model=LayoutBody)
    def get_layout(platform: Platform, login: str) -> LayoutBody:
        return LayoutBody.from_layout(find_streamer(platform, login).layout)

    @app.put("/api/streamers/{platform}/{login}/layout", response_model=LayoutBody)
    def put_layout(platform: Platform, login: str, body: LayoutBody) -> LayoutBody:
        find_streamer(platform, login)
        try:
            layout = body.to_layout()
            editor.set_layout(platform, login, layout)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        log.info("layout saved for %s:%s (%s)", platform.value, login, layout.mode)
        return LayoutBody.from_layout(find_streamer(platform, login).layout)

    @app.post("/api/streamers/{platform}/{login}/detect", response_model=DetectView)
    def detect(platform: Platform, login: str, body: DetectBody, store: Store = Depends(get_store)) -> DetectView:
        find_streamer(platform, login)
        if body.layout.mode != "stack" or body.layout.facecam is None:
            raise HTTPException(422, "a detecção só se aplica ao modo stack")
        candidates = [body.layout.facecam, *body.layout.facecam_alternates]
        source = source_for(store, body.clip, platform, login)
        score = score_candidates(source, candidates)
        if score.chosen is None:
            msg = "nenhuma posição com rosto — este clip sairia em blur"
        else:
            msg = f"CAM {score.chosen + 1} venceu ({score.hits[score.chosen]}/{score.samples} frames com rosto)"
        return DetectView(samples=score.samples, hits=score.hits, chosen=score.chosen, message=msg)

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
