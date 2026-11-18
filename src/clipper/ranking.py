"""Clip scoring and de-duplication of the same moment clipped by many viewers."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from clipper.models import Clip

# Clips younger than this get their velocity computed against this floor, so a
# 2-minute-old clip with 40 views doesn't outrank everything.
MIN_AGE_MINUTES = 5.0
# Two clips of the same VOD whose offsets are within this window are the same moment.
SAME_MOMENT_WINDOW_S = 30
# Each extra viewer clip of the same moment is a strong signal; boost per duplicate.
CLUSTER_BOOST = 0.25


@dataclass(slots=True)
class RankedClip:
    clip: Clip
    score: float
    cluster_size: int = 1
    duplicates: list[Clip] = field(default_factory=list)


def velocity(clip: Clip, now: datetime) -> float:
    """Views per minute since creation."""
    return clip.view_count / max(clip.age_minutes(now), MIN_AGE_MINUTES)


def _moment_key(clip: Clip) -> tuple[str, str] | None:
    if clip.vod_id is None or clip.vod_offset_s is None:
        return None
    return (clip.broadcaster_login, clip.vod_id)


def cluster_moments(clips: Iterable[Clip]) -> list[list[Clip]]:
    """Groups clips that capture the same moment (same VOD, offsets within the window).

    Clips without VOD info (e.g. VOD disabled, Kick) are their own cluster.
    """
    by_vod: dict[tuple[str, str], list[Clip]] = {}
    singles: list[list[Clip]] = []
    for c in clips:
        key = _moment_key(c)
        if key is None:
            singles.append([c])
        else:
            by_vod.setdefault(key, []).append(c)

    clusters: list[list[Clip]] = []
    for group in by_vod.values():
        group.sort(key=lambda c: c.vod_offset_s or 0)
        current: list[Clip] = [group[0]]
        for c in group[1:]:
            anchor = current[-1].vod_offset_s or 0
            if (c.vod_offset_s or 0) - anchor <= SAME_MOMENT_WINDOW_S:
                current.append(c)
            else:
                clusters.append(current)
                current = [c]
        clusters.append(current)
    return clusters + singles


def rank(
    clips: Iterable[Clip], now: datetime, min_views: int | Callable[[Clip], int] = 0
) -> list[RankedClip]:
    """Returns one representative per moment, best first.

    The representative is the most-viewed clip of the cluster; the score sums the
    velocity of every clip in the cluster and adds a boost per duplicate.
    `min_views` may be per clip (e.g. per streamer/platform: Kick clips get far fewer views).
    """
    ranked: list[RankedClip] = []
    for cluster in cluster_moments(clips):
        best = max(cluster, key=lambda c: (c.view_count, -c.created_at.timestamp()))
        total_views = sum(c.view_count for c in cluster)
        threshold = min_views(best) if callable(min_views) else min_views
        if total_views < threshold:
            continue
        base = sum(velocity(c, now) for c in cluster)
        score = base * (1 + CLUSTER_BOOST * (len(cluster) - 1))
        ranked.append(
            RankedClip(
                clip=best,
                score=round(score, 3),
                cluster_size=len(cluster),
                duplicates=[c for c in cluster if c.id != best.id],
            )
        )
    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked
