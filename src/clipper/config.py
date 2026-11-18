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
    gameplay: Rect = Rect(x=0.0, y=0.0, w=1.0, h=1.0)
    facecam_ratio: float = Field(default=0.33, ge=0.15, le=0.5)


Layout = BlurLayout | StackLayout


class Streamer(BaseModel):
    name: str
    platform: Platform
    login: str
    layout: Layout = Field(default_factory=BlurLayout, discriminator="mode")
    enabled: bool = True
    # Overrides CLIPPER_MIN_VIEWS for this streamer (Kick clips get ~10x fewer views than Twitch).
    min_views: int | None = Field(default=None, ge=0)


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
    game_names: str = Field(default="", alias="CLIPPER_GAME_NAMES")

    whisper_model: str = Field(default="small", alias="CLIPPER_WHISPER_MODEL")
    whisper_language: str = Field(default="pt", alias="CLIPPER_WHISPER_LANGUAGE")

    # Hook title (AI-written). Key may also come from an `ant auth login` profile.
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    hook_enabled: bool = Field(default=True, alias="CLIPPER_HOOK_ENABLED")
    hook_model: str = Field(default="claude-opus-5-5", alias="CLIPPER_HOOK_MODEL")

    fonts_dir: Path = Field(default=Path("assets/fonts"), alias="CLIPPER_FONTS_DIR")

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
