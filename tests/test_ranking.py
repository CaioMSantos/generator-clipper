from __future__ import annotations

from datetime import datetime

from clipper.ranking import cluster_moments, rank, velocity
from tests.conftest import make_clip


def test_velocity_uses_min_age_floor(now: datetime) -> None:
    fresh = make_clip(views=50, age_min=1)
    assert velocity(fresh, now) == 50 / 5  # floor of 5 minutes


def test_same_moment_is_clustered_and_best_take_wins(now: datetime) -> None:
    a = make_clip("a", views=100, vod_offset=1000)
    b = make_clip("b", views=300, vod_offset=1020)  # same moment, more views
    c = make_clip("c", views=80, vod_offset=5000)   # different moment
    ranked = rank([a, b, c], now)
    assert [r.clip.id for r in ranked] == ["b", "c"]
    assert ranked[0].cluster_size == 2
    assert [d.id for d in ranked[0].duplicates] == ["a"]


def test_cluster_boost_beats_single_viral_clip(now: datetime) -> None:
    # Five viewers clipping the same moment is a stronger signal than one clip.
    group = [make_clip(f"g{i}", views=60, vod_offset=2000 + i * 5) for i in range(5)]
    single = make_clip("s", views=280, vod_id="v2", vod_offset=10)
    ranked = rank([*group, single], now)
    assert ranked[0].cluster_size == 5


def test_clips_without_vod_are_not_merged(now: datetime) -> None:
    a = make_clip("a", vod_id=None, vod_offset=None)
    b = make_clip("b", vod_id=None, vod_offset=None)
    assert len(cluster_moments([a, b])) == 2


def test_different_streamers_never_merge(now: datetime) -> None:
    a = make_clip("a", login="coringa")
    b = make_clip("b", login="gaules")  # same vod id/offset by coincidence
    assert len(cluster_moments([a, b])) == 2


def test_min_views_filters_on_cluster_total(now: datetime) -> None:
    a = make_clip("a", views=10, vod_offset=100)
    b = make_clip("b", views=25, vod_offset=110)
    assert len(rank([a, b], now, min_views=30)) == 1
    assert len(rank([a], now, min_views=30)) == 0


def test_min_views_can_be_per_clip(now: datetime) -> None:
    kick = make_clip(id="k", views=8)
    twitch = make_clip(id="t", views=8)
    per_clip = rank([kick, twitch], now, min_views=lambda c: 5 if c.id == "k" else 100)
    assert [r.clip.id for r in per_clip] == ["k"]
