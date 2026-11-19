"""SQLite persistence for clip state (idempotent across cycles and restarts)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from clipper.config import Platform
from clipper.models import Clip, ClipStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    key            TEXT PRIMARY KEY,
    id             TEXT NOT NULL,
    platform       TEXT NOT NULL,
    broadcaster    TEXT NOT NULL,
    broadcaster_name TEXT NOT NULL,
    title          TEXT NOT NULL,
    url            TEXT NOT NULL,
    view_count     INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    duration_s     REAL NOT NULL,
    vod_id         TEXT,
    vod_offset_s   INTEGER,
    score          REAL NOT NULL DEFAULT 0,
    cluster_size   INTEGER NOT NULL DEFAULT 1,
    status         TEXT NOT NULL,
    output_path    TEXT,
    error          TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_clips_status_score ON clips(status, score DESC);
"""

MAX_ATTEMPTS = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_utc(dt: datetime) -> str:
    """Normalized UTC ISO string so lexical comparison in SQL matches chronological order."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class ClipRow:
    key: str
    clip: Clip
    score: float
    cluster_size: int
    status: ClipStatus
    output_path: str | None
    error: str | None
    attempts: int
    updated_at: datetime | None = None


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def upsert_discovered(self, clip: Clip, score: float, cluster_size: int) -> None:
        """Inserts a new clip or refreshes views/score of a known one (never resets its status)."""
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO clips (key, id, platform, broadcaster, broadcaster_name, title, url,
                    view_count, created_at, duration_s, vod_id, vod_offset_s, score, cluster_size,
                    status, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    view_count = excluded.view_count,
                    score = excluded.score,
                    cluster_size = excluded.cluster_size,
                    title = excluded.title,
                    -- a clip that was a duplicate may become the best take of its moment
                    status = CASE WHEN clips.status = 'skipped' THEN 'discovered' ELSE clips.status END,
                    updated_at = excluded.updated_at
                """,
                (
                    clip.key, clip.id, clip.platform.value, clip.broadcaster_login,
                    clip.broadcaster_name, clip.title, clip.url, clip.view_count,
                    _iso_utc(clip.created_at), clip.duration_s, clip.vod_id, clip.vod_offset_s,
                    score, cluster_size, ClipStatus.DISCOVERED.value, _now(),
                ),
            )

    def mark_skipped(self, clip: Clip) -> None:
        """Marks a duplicate of an already-chosen moment so it's never rendered."""
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO clips (key, id, platform, broadcaster, broadcaster_name, title, url,
                    view_count, created_at, duration_s, vod_id, vod_offset_s, status, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at
                WHERE clips.status = 'discovered'
                """,
                (
                    clip.key, clip.id, clip.platform.value, clip.broadcaster_login,
                    clip.broadcaster_name, clip.title, clip.url, clip.view_count,
                    _iso_utc(clip.created_at), clip.duration_s, clip.vod_id, clip.vod_offset_s,
                    ClipStatus.SKIPPED.value, _now(),
                ),
            )

    def claim_next(
        self, limit: int, created_after: datetime, per_streamer: int | None = None
    ) -> list[ClipRow]:
        """Atomically moves the best pending clips to RENDERING and returns them.

        Clips older than `created_after` are ignored: their score is stale and the moment is gone.
        With `per_streamer`, at most that many clips per (platform, broadcaster) are claimed and
        the order is round-robin (every streamer's best first), so one heavily-clipped channel
        can't take the whole cycle.
        """
        cap = per_streamer if per_streamer is not None else limit
        with self._tx() as c:
            rows = c.execute(
                """
                SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY platform, broadcaster ORDER BY score DESC
                    ) AS rn
                    FROM clips
                    WHERE (status = 'discovered' OR (status = 'failed' AND attempts < ?))
                      AND created_at >= ?
                )
                WHERE rn <= ?
                ORDER BY rn ASC, score DESC LIMIT ?
                """,
                (MAX_ATTEMPTS, _iso_utc(created_after), cap, limit),
            ).fetchall()
            for r in rows:
                c.execute(
                    "UPDATE clips SET status=?, attempts=attempts+1, updated_at=? WHERE key=?",
                    (ClipStatus.RENDERING.value, _now(), r["key"]),
                )
        return [self._row(r, status_override=ClipStatus.RENDERING) for r in rows]

    def set_status(
        self, key: str, status: ClipStatus, *, output_path: str | None = None, error: str | None = None
    ) -> None:
        with self._tx() as c:
            cur = c.execute(
                """
                UPDATE clips SET status=?, output_path=COALESCE(?, output_path), error=?, updated_at=?
                WHERE key=?
                """,
                (status.value, output_path, error, _now(), key),
            )
            if cur.rowcount == 0:
                raise KeyError(f"clip not found: {key}")

    def recover_stuck(self) -> int:
        """Clips left in RENDERING by a crash go back to the queue."""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE clips SET status='discovered', updated_at=? WHERE status='rendering'", (_now(),)
            )
            return cur.rowcount

    def list_clips(self, status: ClipStatus | None = None, limit: int = 50) -> list[ClipRow]:
        if status is None:
            rows = self._conn.execute(
                "SELECT * FROM clips ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM clips WHERE status=? ORDER BY score DESC LIMIT ?",
                (status.value, limit),
            ).fetchall()
        return [self._row(r) for r in rows]

    def list_by_statuses(
        self, statuses: list[ClipStatus], streamer: str | None = None, limit: int = 200
    ) -> list[ClipRow]:
        """Newest activity first; used by the review panel."""
        if not statuses:
            return []
        placeholders = ",".join("?" * len(statuses))
        sql = f"SELECT * FROM clips WHERE status IN ({placeholders})"
        params: list[object] = [s.value for s in statuses]
        if streamer:
            sql += " AND broadcaster = ?"
            params.append(streamer.lower())
        sql += " ORDER BY updated_at DESC, score DESC LIMIT ?"
        params.append(limit)
        return [self._row(r) for r in self._conn.execute(sql, params).fetchall()]

    def count_by_status(self) -> dict[ClipStatus, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM clips GROUP BY status").fetchall()
        return {ClipStatus(r["status"]): int(r["n"]) for r in rows}

    def count_by_streamer(self, statuses: list[ClipStatus]) -> dict[tuple[str, str], int]:
        """(platform, broadcaster) -> clips in the given statuses."""
        if not statuses:
            return {}
        placeholders = ",".join("?" * len(statuses))
        rows = self._conn.execute(
            f"SELECT platform, broadcaster, COUNT(*) AS n FROM clips WHERE status IN ({placeholders}) "
            "GROUP BY platform, broadcaster",
            [s.value for s in statuses],
        ).fetchall()
        return {(r["platform"], r["broadcaster"]): int(r["n"]) for r in rows}

    def last_activity(self) -> datetime | None:
        r = self._conn.execute("SELECT MAX(updated_at) AS t FROM clips").fetchone()
        return datetime.fromisoformat(r["t"]) if r and r["t"] else None

    def statuses(self, keys: list[str]) -> dict[str, ClipStatus]:
        if not keys:
            return {}
        placeholders = ",".join("?" * len(keys))
        rows = self._conn.execute(
            f"SELECT key, status FROM clips WHERE key IN ({placeholders})", keys
        ).fetchall()
        return {r["key"]: ClipStatus(r["status"]) for r in rows}

    def get(self, key: str) -> ClipRow | None:
        r = self._conn.execute("SELECT * FROM clips WHERE key=?", (key,)).fetchone()
        return self._row(r) if r else None

    @staticmethod
    def _row(r: sqlite3.Row, status_override: ClipStatus | None = None) -> ClipRow:
        clip = Clip(
            id=r["id"],
            platform=Platform(r["platform"]),
            broadcaster_login=r["broadcaster"],
            broadcaster_name=r["broadcaster_name"],
            title=r["title"],
            url=r["url"],
            view_count=r["view_count"],
            created_at=datetime.fromisoformat(r["created_at"]),
            duration_s=r["duration_s"],
            vod_id=r["vod_id"],
            vod_offset_s=r["vod_offset_s"],
        )
        return ClipRow(
            key=r["key"],
            clip=clip,
            score=r["score"],
            cluster_size=r["cluster_size"],
            status=status_override or ClipStatus(r["status"]),
            output_path=r["output_path"],
            error=r["error"],
            attempts=r["attempts"],
            updated_at=datetime.fromisoformat(r["updated_at"]) if r["updated_at"] else None,
        )
