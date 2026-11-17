from __future__ import annotations

from pathlib import Path

import pytest

from clipper.config import BlurLayout, ConfigError, Platform, StackLayout, load_catalog

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config" / "streamers.yaml"


def test_repo_catalog_loads() -> None:
    cat = load_catalog(REPO_CONFIG)
    logins = {s.login for s in cat.enabled()}
    assert {"coringa", "gaules", "alanzoka", "davyjones"} <= logins
    alanzoka = cat.find(Platform.TWITCH, "alanzoka")
    assert alanzoka is not None and isinstance(alanzoka.layout, StackLayout)
    assert all(isinstance(s.layout, (BlurLayout, StackLayout)) for s in cat.streamers)


def test_stack_layout_and_find(tmp_path: Path) -> None:
    f = tmp_path / "s.yaml"
    f.write_text(
        "streamers:\n"
        "  - name: X\n    platform: kick\n    login: Xyz\n"
        "    layout: {mode: stack, facecam: {x: 0.7, y: 0.7, w: 0.3, h: 0.3}}\n"
    )
    cat = load_catalog(f)
    s = cat.find(Platform.KICK, "xyz")
    assert s is not None and isinstance(s.layout, StackLayout)
    assert cat.find(Platform.TWITCH, "xyz") is None


def test_rect_outside_frame_is_rejected(tmp_path: Path) -> None:
    f = tmp_path / "s.yaml"
    f.write_text(
        "streamers:\n  - name: X\n    platform: twitch\n    login: x\n"
        "    layout: {mode: stack, facecam: {x: 0.9, y: 0, w: 0.3, h: 0.3}}\n"
    )
    with pytest.raises(ConfigError, match="fit inside"):
        load_catalog(f)


def test_missing_file() -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_catalog(Path("/nope.yaml"))
