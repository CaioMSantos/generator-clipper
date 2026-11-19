"""Background re-render jobs for the panel (one at a time: renders are CPU-bound)."""

from __future__ import annotations

import itertools
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from clipper.media import MediaError
from clipper.models import ClipStatus
from clipper.pipeline import Pipeline

log = logging.getLogger(__name__)

MAX_KEPT_JOBS = 50


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class RerenderRequest:
    hook_text: str | None = None          # None = ask the AI, "" = no hook
    trim: tuple[float, float] | None = None
    captions: bool = True


@dataclass
class Job:
    id: int
    key: str
    request: RerenderRequest
    state: JobState = JobState.QUEUED
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None


class RerenderQueue:
    def __init__(self, pipeline_factory: Callable[[], Pipeline]) -> None:
        self._factory = pipeline_factory
        self._pipeline: Pipeline | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rerender")
        self._jobs: dict[int, Job] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def submit(self, key: str, request: RerenderRequest) -> Job:
        with self._lock:
            active = [j for j in self._jobs.values() if j.key == key and j.state in (JobState.QUEUED, JobState.RUNNING)]
            if active:
                return active[0]
            job = Job(id=next(self._ids), key=key, request=request)
            self._jobs[job.id] = job
            self._trim_history()
        self._executor.submit(self._run, job)
        return job

    def jobs(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.id, reverse=True)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _trim_history(self) -> None:
        done = [j for j in self._jobs.values() if j.state in (JobState.DONE, JobState.FAILED)]
        for j in sorted(done, key=lambda j: j.id)[: max(len(self._jobs) - MAX_KEPT_JOBS, 0)]:
            del self._jobs[j.id]

    def _run(self, job: Job) -> None:
        job.state = JobState.RUNNING
        try:
            if self._pipeline is None:
                self._pipeline = self._factory()  # loads whisper lazily on first transcription
            pipe = self._pipeline
            row = pipe.store.get(job.key)
            if row is None:
                raise MediaError(f"clip not found: {job.key}")
            pipe.store.set_status(job.key, ClipStatus.RENDERING)
            result = pipe.render_clip(
                row.clip,
                score=row.score,
                captions=job.request.captions,
                hook_text=job.request.hook_text,
                trim=job.request.trim,
            )
            pipe.store.set_status(job.key, ClipStatus.READY, output_path=str(result.output))
            job.state = JobState.DONE
            log.info("panel re-render done: %s", job.key)
        except Exception as exc:  # report any failure to the UI instead of killing the worker
            log.exception("panel re-render failed: %s", job.key)
            job.state = JobState.FAILED
            job.error = str(exc)[:500]
            if self._pipeline is not None:
                try:
                    self._pipeline.store.set_status(job.key, ClipStatus.FAILED, error=job.error)
                except KeyError:
                    pass
        finally:
            job.finished_at = datetime.now(timezone.utc)
