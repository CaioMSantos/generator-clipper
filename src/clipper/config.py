"""Runtime settings (env) and streamer catalog (YAML)."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(Exception):
    """Raised when configuration files are missing or invalid."""


class Platform(str, Enum):
    TWITCH = "twitch"
    KICK = "kick"


class Rect(BaseModel):
    """Normalized rectangle (0..1) relative to the source frame."""

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _fits_in_frame(self) -> Rect:
        if self.x + self.w > 1.0001 or self.y + self.h > 1.0001:
            raise ValueError("rect must fit inside the frame (x+w <= 1 and y+h <= 1)")
        return self


class BlurLayout(BaseModel):
    mode: Literal["blur"] = "blur"
    zoom: float = Field(default=1.0, ge=1.0, le=2.0)


class StackLayout(BaseModel):
    mode: Literal["stack"]
    facecam: Rect
    # Other places the streamer puts the camera in other scenes; the one with a detected face wins.
    facecam_alternates: list[Rect] = Field(default_factory=list)
    gameplay: Rect = Rect(x=0.0, y=0.0, w=1.0, h=1.0)
    facecam_ratio: float = Field(default=0.33, ge=0.15, le=0.5)


Layout = BlurLayout | StackLayout


# ---- output templates ("moldes") ----------------------------------------------
# A template places sources on the 1080x1920 output. Rects here are normalized to the
# OUTPUT canvas (the calibration Rects above are normalized to the SOURCE frame).

_HEX = r"^#[0-9a-fA-F]{6}$"
_SLUG = r"^[a-z0-9][a-z0-9\-]{1,39}$"


class SlotSpec(BaseModel):
    kind: Literal["cam", "game"]
    dst: Rect
    fit: Literal["cover", "contain"] = "cover"
    shape: Literal["rect", "rounded", "circle"] = "rect"
    border_px: int = Field(default=0, ge=0, le=40)
    border_color: str = Field(default="#ffffff", pattern=_HEX)


class Template(BaseModel):
    id: str = Field(pattern=_SLUG)
    name: str = Field(min_length=1, max_length=60)
    background: Literal["black", "blur"] = "black"
    slots: list[SlotSpec] = Field(min_length=1, max_length=4)
    hook_y: float = Field(default=0.30, ge=0.05, le=0.95)     # hook card center
    caption_y: float = Field(default=0.83, ge=0.30, le=0.95)  # caption baseline

    @property
    def needs_cam(self) -> bool:
        return any(s.kind == "cam" for s in self.slots)


class Streamer(BaseModel):
    name: str
    platform: Platform
    login: str
    layout: Layout = Field(default_factory=BlurLayout, discriminator="mode")
    enabled: bool = True
    # Overrides CLIPPER_MIN_VIEWS for this streamer (Kick clips get ~10x fewer views than Twitch).
    min_views: int | None = Field(default=None, ge=0)
    # Output template id ("molde"); None keeps the classic layout-driven render.
    template: str | None = None


class StreamerCatalog(BaseModel):
    streamers: list[Streamer]

    def enabled(self, platform: Platform | None = None) -> list[Streamer]:
        return [
            s for s in self.streamers if s.enabled and (platform is None or s.platform == platform)
        ]

    def find(self, platform: Platform, login: str) -> Streamer | None:
        login = login.lower()
        return next(
            (s for s in self.streamers if s.platform == platform and s.login.lower() == login),
            None,
        )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    twitch_client_id: str = ""
    twitch_client_secret: str = ""

    streamers_file: Path = Field(default=Path("config/streamers.yaml"), alias="CLIPPER_STREAMERS_FILE")
    data_dir: Path = Field(default=Path("data"), alias="CLIPPER_DATA_DIR")
    # Where finished .mp4/.json go (date subfolders). Empty = <data_dir>/output.
    output_dir_override: Path | None = Field(default=None, alias="CLIPPER_OUTPUT_DIR")

    poll_interval_seconds: int = Field(default=120, ge=30, alias="CLIPPER_POLL_INTERVAL_SECONDS")
    lookback_hours: int = Field(default=6, ge=1, le=72, alias="CLIPPER_LOOKBACK_HOURS")
    min_views: int = Field(default=30, ge=0, alias="CLIPPER_MIN_VIEWS")
    max_renders_per_cycle: int = Field(default=4, ge=1, alias="CLIPPER_MAX_RENDERS_PER_CYCLE")
    # Longer clips are dropped (Kick allows up to 3 min; Shorts/TikTok cuts work best short).
    # 0 = no limit. Clips with unknown duration (0) are kept.
    max_clip_seconds: float = Field(default=90, ge=0, alias="CLIPPER_MAX_CLIP_SECONDS")
    # Fairness: one heavily-clipped channel can't take the whole cycle (0 = no per-streamer cap).
    max_renders_per_streamer: int = Field(default=2, ge=0, alias="CLIPPER_MAX_RENDERS_PER_STREAMER")
    game_names: str = Field(default="", alias="CLIPPER_GAME_NAMES")

    whisper_model: str = Field(default="small", alias="CLIPPER_WHISPER_MODEL")
    whisper_language: str = Field(default="pt", alias="CLIPPER_WHISPER_LANGUAGE")

    # Hook title (AI-written). Key may also come from an `ant auth login` profile.
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    hook_enabled: bool = Field(default=True, alias="CLIPPER_HOOK_ENABLED")
    hook_model: str = Field(default="claude-opus-5-5", alias="CLIPPER_HOOK_MODEL")

    fonts_dir: Path = Field(default=Path("assets/fonts"), alias="CLIPPER_FONTS_DIR")
    templates_file: Path = Field(default=Path("config/templates.yaml"), alias="CLIPPER_TEMPLATES_FILE")

    # Find the facecam per clip (face detection over the candidate rects); no face -> blur layout.
    facecam_detect: bool = Field(default=True, alias="CLIPPER_FACECAM_DETECT")

    # Extra Host headers the panel accepts besides localhost (e.g. a LAN IP). Comma-separated.
    panel_allowed_hosts: str = Field(default="", alias="CLIPPER_PANEL_ALLOWED_HOSTS")

    @property
    def panel_allowed_host_set(self) -> set[str]:
        return {h.strip().lower() for h in self.panel_allowed_hosts.split(",") if h.strip()}

    @property
    def game_name_list(self) -> list[str]:
        return [g.strip() for g in self.game_names.split(",") if g.strip()]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clipper.sqlite3"

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @field_validator("output_dir_override", mode="before")
    @classmethod
    def _empty_output_dir_is_default(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def output_dir(self) -> Path:
        return self.output_dir_override or self.data_dir / "output"


def load_catalog(path: Path) -> StreamerCatalog:
    if not path.is_file():
        raise ConfigError(f"streamers file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return StreamerCatalog.model_validate(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    except ValidationError as exc:
        raise ConfigError(f"invalid streamers config in {path}:\n{exc}") from exc
