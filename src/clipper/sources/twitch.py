"""Twitch Helix clip source (app access token / client credentials)."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

Params = list[tuple[str, str | int | float | bool | None]]

import httpx

from clipper.config import Platform, Streamer
from clipper.models import Clip
from clipper.sources import SourceError

log = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX_URL = "https://api.twitch.tv/helix"
MAX_PAGES_PER_STREAMER = 5  # 5 * 100 clips is plenty for a lookback of a few hours


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TwitchSource:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        game_names: list[str] | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise SourceError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are required")
        self._client_id = client_id
        self._client_secret = client_secret
        self._game_names = game_names or []
        self._http = http or httpx.Client(timeout=httpx.Timeout(15.0))
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._user_ids: dict[str, str] = {}
        self._game_ids: set[str] | None = None

    # ---- auth -------------------------------------------------------------

    def _ensure_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at - 60:
            return self._token
        resp = self._http.post(
            TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "client_credentials",
            },
        )
        if resp.status_code != 200:
            raise SourceError(f"twitch auth failed ({resp.status_code}): {resp.text[:200]}")
        body = resp.json()
        self._token = str(body["access_token"])
        self._token_expires_at = time.monotonic() + float(body.get("expires_in", 3600))
        return self._token

    def _get(self, path: str, params: Params) -> dict[str, Any]:
        for attempt in range(3):
            token = self._ensure_token()
            resp = self._http.get(
                f"{HELIX_URL}{path}",
                params=params,
                headers={"Client-Id": self._client_id, "Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 401:
                self._token = None  # token revoked/expired: refresh once
                continue
            if resp.status_code == 429:
                reset = float(resp.headers.get("Ratelimit-Reset", time.time() + 2))
                wait = min(max(reset - time.time(), 1.0), 30.0)
                log.warning("twitch rate limited, sleeping %.1fs", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500 and attempt < 2:
                time.sleep(2**attempt)
                continue
            if resp.status_code != 200:
                raise SourceError(f"GET {path} failed ({resp.status_code}): {resp.text[:200]}")
            data: dict[str, Any] = resp.json()
            return data
        raise SourceError(f"GET {path} failed after retries")

    # ---- lookups ----------------------------------------------------------

    def _resolve_user_ids(self, logins: list[str]) -> dict[str, str]:
        missing = [lg for lg in logins if lg not in self._user_ids]
        for i in range(0, len(missing), 100):
            chunk = missing[i : i + 100]
            body = self._get("/users", [("login", lg) for lg in chunk])
            for user in body.get("data", []):
                self._user_ids[str(user["login"]).lower()] = str(user["id"])
        for lg in missing:
            if lg not in self._user_ids:
                log.warning("twitch user not found: %s", lg)
        return {lg: self._user_ids[lg] for lg in logins if lg in self._user_ids}

    def _resolve_game_ids(self) -> set[str] | None:
        """Returns allowed game ids, or None when no filter is configured/resolvable."""
        if not self._game_names:
            return None
        if self._game_ids is None:
            body = self._get("/games", [("name", n) for n in self._game_names])
            ids = {str(g["id"]) for g in body.get("data", [])}
            if not ids:
                # Game may not exist in Twitch's catalog yet (pre-launch): don't block everything.
                log.warning("none of the games %s found on twitch; game filter disabled", self._game_names)
                return None
            self._game_ids = ids
        return self._game_ids

    # ---- public -----------------------------------------------------------

    def fetch_clips(self, streamers: list[Streamer], since: datetime) -> list[Clip]:
        twitch_streamers = [s for s in streamers if s.platform == Platform.TWITCH]
        if not twitch_streamers:
            return []
        logins = [s.login.lower() for s in twitch_streamers]
        user_ids = self._resolve_user_ids(logins)
        game_ids = self._resolve_game_ids()

        clips: list[Clip] = []
        for login, broadcaster_id in user_ids.items():
            try:
                clips.extend(self._fetch_for_broadcaster(login, broadcaster_id, since, game_ids))
            except SourceError as exc:
                # One broken streamer must not kill the whole cycle.
                log.error("failed to fetch clips for %s: %s", login, exc)
        return clips

    def _fetch_for_broadcaster(
        self, login: str, broadcaster_id: str, since: datetime, game_ids: set[str] | None
    ) -> list[Clip]:
        out: list[Clip] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES_PER_STREAMER):
            params: Params = [
                ("broadcaster_id", broadcaster_id),
                ("started_at", _rfc3339(since)),
                ("first", "100"),
            ]
            if cursor:
                params.append(("after", cursor))
            body = self._get("/clips", params)
            for raw in body.get("data", []):
                game_id = str(raw.get("game_id") or "") or None
                if game_ids is not None and game_id not in game_ids:
                    continue
                out.append(self._to_clip(login, raw))
            cursor = (body.get("pagination") or {}).get("cursor")
            if not cursor:
                break
        log.info("twitch %s: %d clips since %s", login, len(out), _rfc3339(since))
        return out

    @staticmethod
    def _to_clip(login: str, raw: dict[str, Any]) -> Clip:
        vod_offset = raw.get("vod_offset")
        return Clip(
            id=str(raw["id"]),
            platform=Platform.TWITCH,
            broadcaster_login=login,
            broadcaster_name=str(raw.get("broadcaster_name") or login),
            title=str(raw.get("title") or ""),
            url=str(raw["url"]),
            view_count=int(raw.get("view_count") or 0),
            created_at=_parse_ts(str(raw["created_at"])),
            duration_s=float(raw.get("duration") or 0),
            vod_id=str(raw.get("video_id") or "") or None,
            vod_offset_s=int(vod_offset) if vod_offset is not None else None,
            game_id=str(raw.get("game_id") or "") or None,
        )
