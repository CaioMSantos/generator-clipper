"""Connections page backend: credentials in .env, connection tests, streamer catalog edits.

Secrets are written to `.env` (gitignored) and never sent back to the browser: the API only
returns whether a key is set plus its last 4 characters. The streamer catalog is edited with
ruamel.yaml round-trip so the calibration comments in streamers.yaml survive every save.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import anthropic
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from clipper.config import BlurLayout, ConfigError, Layout, Platform, Rect, StackLayout, StreamerCatalog, load_catalog
from clipper.sources import SourceError
from clipper.sources.kick import KickSource
from clipper.sources.twitch import TwitchSource

log = logging.getLogger(__name__)

ConnectionId = Literal["twitch", "kick", "anthropic"]
_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]{1,39}$")


# ---- .env -------------------------------------------------------------------


class EnvStore:
    """Minimal .env editor: keeps comments, blank lines and key order; atomic writes."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        out: dict[str, str] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            m = _KEY_RE.match(line)
            if m:
                value = line.split("=", 1)[1].strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                out[m.group(1)] = value
        return out

    def update(self, values: dict[str, str]) -> None:
        for key, value in values.items():
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
                raise ValueError(f"invalid env key: {key!r}")
            if "\n" in value or "\r" in value:
                raise ValueError(f"{key} must be a single line")
        lines = self.path.read_text(encoding="utf-8").splitlines() if self.path.is_file() else []
        pending = dict(values)
        for i, line in enumerate(lines):
            m = _KEY_RE.match(line)
            if m and m.group(1) in pending:
                lines[i] = f"{m.group(1)}={pending.pop(m.group(1))}"
        lines.extend(f"{k}={v}" for k, v in pending.items())
        _atomic_write(self.path, "\n".join(lines) + "\n")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def mask(value: str) -> str | None:
    if not value:
        return None
    return "••••" + value[-4:] if len(value) > 8 else "••••"


# ---- connection registry ----------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    secret: bool = True
    help: str = ""


@dataclass(frozen=True)
class ConnectionSpec:
    id: ConnectionId
    name: str
    description: str
    fields: tuple[FieldSpec, ...]
    docs_url: str
    required: bool = True


CONNECTIONS: tuple[ConnectionSpec, ...] = (
    ConnectionSpec(
        id="twitch",
        name="Twitch",
        description="Helix API (app token). Lê os clips dos canais da Twitch.",
        fields=(
            FieldSpec("TWITCH_CLIENT_ID", "Client ID", secret=False),
            FieldSpec("TWITCH_CLIENT_SECRET", "Client Secret"),
        ),
        docs_url="https://dev.twitch.tv/console/apps",
    ),
    ConnectionSpec(
        id="kick",
        name="Kick",
        description="Endpoint público do site, sem credencial. Pode dar 403 a partir de datacenter.",
        fields=(),
        docs_url="https://kick.com",
    ),
    ConnectionSpec(
        id="anthropic",
        name="Claude (Anthropic)",
        description="Gera o título-gancho de cada corte. Opcional: sem chave, sai sem gancho.",
        fields=(FieldSpec("ANTHROPIC_API_KEY", "API key"),),
        docs_url="https://console.anthropic.com/settings/keys",
        required=False,
    ),
)
SPECS: dict[str, ConnectionSpec] = {c.id: c for c in CONNECTIONS}


@dataclass
class TestResult:
    ok: bool
    message: str
    latency_ms: int
    at: float = field(default_factory=time.time)


def _timed(fn: Callable[[], str]) -> TestResult:
    started = time.perf_counter()
    try:
        msg = fn()
        ok = True
    except Exception as exc:  # surfaced to the UI as the test result
        msg, ok = str(exc)[:300] or exc.__class__.__name__, False
    return TestResult(ok=ok, message=msg, latency_ms=int((time.perf_counter() - started) * 1000))


class ConnectionTester:
    """Runs a live check per connection. Factories are injectable for tests."""

    def __init__(
        self,
        twitch_factory: Callable[[str, str], TwitchSource] = TwitchSource,
        kick_factory: Callable[[], KickSource] = KickSource,
        anthropic_factory: Callable[[str], anthropic.Anthropic] = lambda key: anthropic.Anthropic(
            api_key=key, timeout=15.0, max_retries=0
        ),
    ) -> None:
        self.twitch_factory = twitch_factory
        self.kick_factory = kick_factory
        self.anthropic_factory = anthropic_factory
        self.last: dict[str, TestResult] = {}

    def run(self, conn: ConnectionId, env: dict[str, str], kick_probe: str = "kick") -> TestResult:
        def twitch() -> str:
            cid, secret = env.get("TWITCH_CLIENT_ID", ""), env.get("TWITCH_CLIENT_SECRET", "")
            if not cid or not secret:
                raise SourceError("preencha Client ID e Client Secret")
            self.twitch_factory(cid, secret).check_credentials()
            return "token de app emitido"

        def kick() -> str:
            if not self.kick_factory().channel_exists(kick_probe):
                return f"Kick respondeu, mas o canal '{kick_probe}' não existe"
            return f"Kick acessível (canal '{kick_probe}' encontrado)"

        def claude() -> str:
            key = env.get("ANTHROPIC_API_KEY", "")
            if not key:
                raise SourceError("preencha a API key")
            try:
                page = self.anthropic_factory(key).models.list(limit=1)
            except anthropic.AuthenticationError as exc:
                raise SourceError("chave inválida (401)") from exc
            except anthropic.PermissionDeniedError as exc:
                raise SourceError("chave sem permissão (403)") from exc
            except anthropic.APIConnectionError as exc:
                raise SourceError(f"sem conexão com a API: {exc}") from exc
            first = next(iter(page.data), None)
            return f"chave válida{f' (ex.: {first.id})' if first else ''}"

        runners: dict[str, Callable[[], str]] = {"twitch": twitch, "kick": kick, "anthropic": claude}
        result = _timed(runners[conn])
        self.last[conn] = result
        log.info("connection test %s: ok=%s %s", conn, result.ok, result.message)
        return result


# ---- streamer catalog edits -------------------------------------------------


class CatalogEditor:
    """Round-trip edits of streamers.yaml; validates by re-loading through the real schema."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._yaml = YAML()
        self._yaml.preserve_quotes = True
        self._yaml.indent(mapping=2, sequence=4, offset=2)
        self._yaml.width = 4096

    def load(self) -> StreamerCatalog:
        return load_catalog(self.path)

    def _doc(self) -> Any:
        with self.path.open(encoding="utf-8") as f:
            return self._yaml.load(f)

    def _save(self, doc: Any) -> None:
        from io import StringIO

        buf = StringIO()
        self._yaml.dump(doc, buf)
        text = buf.getvalue()
        # Validate before touching the real file: a broken catalog would stop the loop.
        probe = self.path.with_name(f".{self.path.name}.check")
        try:
            probe.write_text(text, encoding="utf-8")
            load_catalog(probe)
        except ConfigError as exc:
            raise ValueError(str(exc)) from exc
        finally:
            probe.unlink(missing_ok=True)
        _atomic_write(self.path, text)

    @staticmethod
    def _find(doc: Any, platform: Platform, login: str) -> int | None:
        for i, item in enumerate(doc["streamers"]):
            if str(item.get("platform")) == platform.value and str(item.get("login", "")).lower() == login.lower():
                return i
        return None

    def add(self, name: str, platform: Platform, login: str, min_views: int | None) -> None:
        login = login.strip().lower()
        if not _LOGIN_RE.match(login):
            raise ValueError("login inválido: use o slug da URL do canal (letras, números, _ e -)")
        if not name.strip():
            raise ValueError("nome obrigatório")
        doc = self._doc()
        if self._find(doc, platform, login) is not None:
            raise ValueError(f"{login} já está cadastrado na {platform.value}")
        entry = CommentedMap()
        entry["name"] = name.strip()
        entry["platform"] = platform.value
        entry["login"] = login
        if min_views is not None:
            entry["min_views"] = min_views
        layout = CommentedMap()
        layout["mode"] = "blur"
        entry["layout"] = layout
        doc["streamers"].append(entry)
        doc["streamers"].yaml_set_comment_before_after_key(
            len(doc["streamers"]) - 1, before="Adicionado pelo painel. Calibre a facecam com `clipper frame <url>`.", indent=2
        )
        self._save(doc)

    def update(
        self, platform: Platform, login: str, *, enabled: bool | None = None,
        min_views: int | None | Literal["unset"] = "unset", name: str | None = None,
    ) -> None:  # fmt: skip
        doc = self._doc()
        idx = self._find(doc, platform, login)
        if idx is None:
            raise KeyError(f"{platform.value}:{login} não encontrado")
        item = doc["streamers"][idx]
        if enabled is not None:
            if enabled and "enabled" in item:
                del item["enabled"]  # default is enabled; keep the YAML clean
            elif not enabled:
                item["enabled"] = False
        if min_views != "unset":
            if min_views is None:
                item.pop("min_views", None)
            else:
                item["min_views"] = min_views
        if name is not None and name.strip():
            item["name"] = name.strip()
        self._save(doc)

    @staticmethod
    def _rect_map(r: Rect) -> CommentedMap:
        m = CommentedMap()
        for k in ("x", "y", "w", "h"):
            m[k] = round(float(getattr(r, k)), 4)
        m.fa.set_flow_style()  # keep `{x: .., y: .., w: .., h: ..}` on one line
        return m

    def set_layout(self, platform: Platform, login: str, layout: Layout) -> None:
        """Replaces the streamer's layout, editing the existing node so comments around it stay."""
        doc = self._doc()
        idx = self._find(doc, platform, login)
        if idx is None:
            raise KeyError(f"{platform.value}:{login} não encontrado")
        item = doc["streamers"][idx]
        node = item.get("layout")
        if not isinstance(node, CommentedMap):
            node = CommentedMap()
            item["layout"] = node
        node["mode"] = layout.mode
        if isinstance(layout, StackLayout):
            node["facecam"] = self._rect_map(layout.facecam)
            if layout.facecam_alternates:
                from ruamel.yaml.comments import CommentedSeq

                seq = CommentedSeq([self._rect_map(r) for r in layout.facecam_alternates])
                node["facecam_alternates"] = seq
            else:
                node.pop("facecam_alternates", None)
            full = Rect(x=0.0, y=0.0, w=1.0, h=1.0)
            if layout.gameplay != full:
                node["gameplay"] = self._rect_map(layout.gameplay)
            else:
                node.pop("gameplay", None)
            node["facecam_ratio"] = round(layout.facecam_ratio, 3)
            node.pop("zoom", None)
        elif isinstance(layout, BlurLayout):
            for key in ("facecam", "facecam_alternates", "gameplay", "facecam_ratio"):
                node.pop(key, None)
            if layout.zoom != 1.0:
                node["zoom"] = layout.zoom
            else:
                node.pop("zoom", None)
        self._save(doc)

    def remove(self, platform: Platform, login: str) -> None:
        doc = self._doc()
        idx = self._find(doc, platform, login)
        if idx is None:
            raise KeyError(f"{platform.value}:{login} não encontrado")
        del doc["streamers"][idx]
        self._save(doc)
