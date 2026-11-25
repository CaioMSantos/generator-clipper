from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clipper.config import Platform, Settings, StreamerCatalog, load_catalog
from clipper.panel.app import create_app
from clipper.panel.connections import CatalogEditor, ConnectionTester, EnvStore, mask
from clipper.sources import SourceError

ENV = """\
# Twitch app
TWITCH_CLIENT_ID=abc123
TWITCH_CLIENT_SECRET=supersecretvalue9f3a

CLIPPER_MIN_VIEWS=30
"""

YAML = """\
# header comment
streamers:
  # keep me: calibrated 17/11
  - name: Alanzoka
    platform: twitch
    login: alanzoka
    layout:
      mode: stack
      facecam: { x: 0.0, y: 0.0, w: 0.2, h: 0.2 }
      facecam_alternates:
        - { x: 0.8, y: 0.0, w: 0.2, h: 0.2 }
"""


class FakeTwitch:
    known = {"alanzoka", "gaules"}

    def __init__(self, cid: str, secret: str) -> None:
        if not cid or not secret:
            raise SourceError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are required")
        self.secret = secret

    def check_credentials(self) -> None:
        if self.secret != "supersecretvalue9f3a":
            raise SourceError("twitch auth failed (403)")

    def channel_exists(self, login: str) -> bool:
        return login in self.known


class FakeKick:
    blocked = False

    def channel_exists(self, slug: str) -> bool:
        if FakeKick.blocked:
            raise SourceError("kick returned 403 (Cloudflare)")
        return slug in {"davyjones", "kick"}


def _anthropic_factory(key: str) -> Any:
    def list_models(limit: int) -> Any:
        if key != "sk-good":
            import anthropic
            import httpx2

            req = httpx2.Request("GET", "https://api.anthropic.com/v1/models")
            raise anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=req), body=None)
        return SimpleNamespace(data=[SimpleNamespace(id="claude-opus-5-5")])

    return SimpleNamespace(models=SimpleNamespace(list=list_models))


@pytest.fixture
def client(tmp_path: Path) -> tuple[TestClient, Path, Path]:
    env_path = tmp_path / ".env"
    env_path.write_text(ENV, encoding="utf-8")
    yaml_path = tmp_path / "streamers.yaml"
    yaml_path.write_text(YAML, encoding="utf-8")
    settings = Settings(CLIPPER_DATA_DIR=tmp_path, CLIPPER_STREAMERS_FILE=yaml_path)
    tester = ConnectionTester(
        twitch_factory=FakeTwitch,  # type: ignore[arg-type]
        kick_factory=FakeKick,  # type: ignore[arg-type]
        anthropic_factory=_anthropic_factory,
    )
    FakeKick.blocked = False
    app = create_app(
        settings, StreamerCatalog(streamers=[]), lambda: None,  # type: ignore[arg-type, return-value]
        env_path=env_path, tester=tester,
    )  # fmt: skip
    return TestClient(app, headers={"X-Clipper-Panel": "1"}), env_path, yaml_path


# ---- EnvStore ------------------------------------------------------------------


def test_env_store_updates_in_place_and_appends(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text(ENV, encoding="utf-8")
    store = EnvStore(p)
    store.update({"TWITCH_CLIENT_ID": "new", "ANTHROPIC_API_KEY": "sk-x"})
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# Twitch app\nTWITCH_CLIENT_ID=new\n")
    assert "CLIPPER_MIN_VIEWS=30" in text and text.endswith("ANTHROPIC_API_KEY=sk-x\n")
    assert store.read()["TWITCH_CLIENT_SECRET"] == "supersecretvalue9f3a"
    with pytest.raises(ValueError):
        store.update({"X": "a\nB=evil"})


def test_mask_never_reveals_short_secrets() -> None:
    assert mask("supersecretvalue9f3a") == "••••9f3a"
    assert mask("short") == "••••"
    assert mask("") is None


# ---- connections API -----------------------------------------------------------


def test_list_connections_masks_secrets(client: tuple[TestClient, Path, Path]) -> None:
    c, _, _ = client
    conns = {x["id"]: x for x in c.get("/api/connections").json()}
    twitch = {f["key"]: f for f in conns["twitch"]["fields"]}
    assert conns["twitch"]["configured"] is True
    assert twitch["TWITCH_CLIENT_ID"]["preview"] == "abc123"  # not a secret
    assert twitch["TWITCH_CLIENT_SECRET"]["preview"] == "••••9f3a"
    assert "supersecretvalue9f3a" not in c.get("/api/connections").text
    assert conns["anthropic"]["configured"] is False and conns["kick"]["fields"] == []


def test_update_and_clear_connection(client: tuple[TestClient, Path, Path]) -> None:
    c, env_path, _ = client
    r = c.put("/api/connections/anthropic", json={"values": {"ANTHROPIC_API_KEY": "sk-good"}})
    assert r.status_code == 200 and r.json()["configured"] is True
    assert "ANTHROPIC_API_KEY=sk-good" in env_path.read_text(encoding="utf-8")
    r = c.put("/api/connections/anthropic", json={"values": {"ANTHROPIC_API_KEY": None}})
    assert r.json()["configured"] is False
    bad = c.put("/api/connections/twitch", json={"values": {"ANTHROPIC_API_KEY": "x"}})
    assert bad.status_code == 422


def test_connection_tests(client: tuple[TestClient, Path, Path]) -> None:
    c, _, _ = client
    assert c.post("/api/connections/twitch/test").json()["ok"] is True
    c.put("/api/connections/twitch", json={"values": {"TWITCH_CLIENT_SECRET": "wrong"}})
    r = c.post("/api/connections/twitch/test").json()
    assert r["ok"] is False and "403" in r["message"]
    assert c.post("/api/connections/anthropic/test").json()["ok"] is False  # not configured
    c.put("/api/connections/anthropic", json={"values": {"ANTHROPIC_API_KEY": "sk-bad"}})
    assert "inválida" in c.post("/api/connections/anthropic/test").json()["message"]
    c.put("/api/connections/anthropic", json={"values": {"ANTHROPIC_API_KEY": "sk-good"}})
    assert c.post("/api/connections/anthropic/test").json()["ok"] is True
    FakeKick.blocked = True
    assert "403" in c.post("/api/connections/kick/test").json()["message"]
    # last result shows up on the list
    kick = next(x for x in c.get("/api/connections").json() if x["id"] == "kick")
    assert kick["last_test"]["ok"] is False


# ---- streamers API -------------------------------------------------------------


def test_add_patch_remove_streamer_keeps_yaml_comments(client: tuple[TestClient, Path, Path]) -> None:
    c, _, yaml_path = client
    r = c.post("/api/streamers", json={"name": "Davy Jones", "platform": "kick", "login": "DavyJones", "min_views": 5})
    assert r.status_code == 201
    assert {(s["platform"], s["login"]) for s in r.json()} == {("twitch", "alanzoka"), ("kick", "davyjones")}
    text = yaml_path.read_text(encoding="utf-8")
    assert "# keep me: calibrated 17/11" in text and "# header comment" in text

    r = c.patch("/api/streamers/kick/davyjones", json={"enabled": False, "min_views": 20})
    davy = next(s for s in r.json() if s["login"] == "davyjones")
    assert (davy["enabled"], davy["min_views"]) == (False, 20)
    r = c.patch("/api/streamers/kick/davyjones", json={"enabled": True, "clear_min_views": True})
    davy = next(s for s in r.json() if s["login"] == "davyjones")
    assert (davy["enabled"], davy["min_views"]) == (True, None)

    alan = next(s for s in c.get("/api/streamers").json() if s["login"] == "alanzoka")
    assert (alan["layout"], alan["facecam_positions"]) == ("stack", 2)

    assert c.delete("/api/streamers/kick/davyjones").status_code == 200
    assert len(load_catalog(yaml_path).streamers) == 1
    assert "# keep me: calibrated 17/11" in yaml_path.read_text(encoding="utf-8")
    assert c.delete("/api/streamers/kick/davyjones").status_code == 404


def test_add_streamer_validation(client: tuple[TestClient, Path, Path]) -> None:
    c, _, _ = client
    nope = c.post("/api/streamers", json={"name": "X", "platform": "twitch", "login": "doesnotexist"})
    assert nope.status_code == 404
    dup = c.post("/api/streamers", json={"name": "A", "platform": "twitch", "login": "alanzoka"})
    assert dup.status_code == 422
    FakeKick.blocked = True
    blocked = c.post("/api/streamers", json={"name": "Y", "platform": "kick", "login": "someone"})
    assert blocked.status_code == 424
    forced = c.post("/api/streamers", json={"name": "Y", "platform": "kick", "login": "someone", "verify": False})
    assert forced.status_code == 201
    bad = c.post("/api/streamers", json={"name": "Z", "platform": "kick", "login": "a b", "verify": False})
    assert bad.status_code == 422


def test_catalog_editor_rejects_changes_that_break_the_schema(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text(YAML, encoding="utf-8")
    editor = CatalogEditor(p)
    with pytest.raises(ValueError):
        editor.update(Platform.TWITCH, "alanzoka", min_views=-1)
    assert p.read_text(encoding="utf-8") == YAML  # untouched on failure


# ---- security --------------------------------------------------------------------


def test_writes_need_panel_header_and_same_origin(client: tuple[TestClient, Path, Path]) -> None:
    c, env_path, _ = client
    before = env_path.read_text(encoding="utf-8")
    no_header = c.put(
        "/api/connections/anthropic", json={"values": {"ANTHROPIC_API_KEY": "sk"}}, headers={"X-Clipper-Panel": ""}
    )
    assert no_header.status_code == 403
    cross = c.put(
        "/api/connections/anthropic", json={"values": {"ANTHROPIC_API_KEY": "sk"}},
        headers={"Origin": "https://evil.example"},
    )  # fmt: skip
    assert cross.status_code == 403
    assert env_path.read_text(encoding="utf-8") == before
    assert c.get("/api/connections").status_code == 200  # reads don't need the header


def test_foreign_host_header_is_rejected(client: tuple[TestClient, Path, Path]) -> None:
    c, _, _ = client
    assert c.get("/api/connections", headers={"Host": "attacker.example"}).status_code == 403
    assert c.get("/api/connections", headers={"Host": "127.0.0.1:8000"}).status_code == 200


# ---- hot reload ------------------------------------------------------------------


def test_config_fingerprint_changes_when_files_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from clipper import cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
    yaml_path = tmp_path / "s.yaml"
    yaml_path.write_text(YAML, encoding="utf-8")
    settings = Settings(CLIPPER_STREAMERS_FILE=yaml_path)
    before = cli.config_fingerprint(settings)
    os.utime(yaml_path, (before[1] + 10, before[1] + 10))
    assert cli.config_fingerprint(settings) != before
