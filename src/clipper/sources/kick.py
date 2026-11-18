"""Kick clip source.

Kick's official public API (api.kick.com/public/v1) does not expose clips, so this uses the
internal endpoint the website uses (validated against real responses on 2026-11-18):

    GET kick.com/api/v2/channels/{slug}/clips?sort=view&time=day[&cursor=...]
    -> {"clips": [...], "nextCursor": "<opaque>" | null}

It sits behind Cloudflare: residential IPs work, datacenter IPs may get 403. Failures are logged
per channel and the cycle continues with the other sources.

Kick clips carry no VOD offset (`vod_starts_at` is always 0), so they never cluster.

We page by most viewed, not by date: a live GTA channel easily makes 200+ clips in 6h and the
date order would truncate before reaching the clips that actually pass `min_views`.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from clipper.config import Platform, Streamer
from clipper.models import Clip

log = logging.getLogger(__name__)

CLIPS_URL = "https://kick.com/api/v2/channels/{slug}/clips"
CLIP_PAGE_URL = "https://kick.com/{slug}/clips/{clip_id}"  # what yt-dlp's Kick extractor expects
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36",
}
MAX_PAGES_PER_CHANNEL = 5  # 20 clips per page, most viewed first
MAX_RETRIES = 3


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def matches_game(category_name: str, game_names: list[str]) -> bool:
    """Kick names categories like "Grand Theft Auto VI (GTA)"; Twitch uses "Grand Theft Auto VI"."""
    name = category_name.strip().lower()
    return any(name == g.lower() or name.startswith(g.lower() + " (") for g in game_names)


class KickSource:
    def __init__(self, game_names: list[str] | None = None, http: httpx.Client | None = None) -> None:
        self._games = [g for g in (game_names or []) if g.strip()]
        self._http = http or httpx.Client(timeout=httpx.Timeout(15.0), headers=HEADERS)

    def fetch_clips(self, streamers: list[Streamer], since: datetime) -> list[Clip]:
        out: list[Clip] = []
        for s in streamers:
            if s.platform != Platform.KICK:
                continue
            try:
                out.extend(self._fetch_for_channel(s.login.lower(), since))
            except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
                # One broken channel must not kill the whole cycle.
                log.error("kick %s: failed to fetch clips: %s", s.login, exc)
        return out

    def _get(self, slug: str, params: dict[str, str]) -> dict[str, Any] | None:
        for attempt in range(MAX_RETRIES):
            resp = self._http.get(CLIPS_URL.format(slug=slug), params=params)
            if resp.status_code == 403:
                log.warning("kick %s: 403 (cloudflare). Rode de um IP residencial ou use proxy.", slug)
                return None
            if resp.status_code == 404:
                log.warning("kick %s: channel not found", slug)
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2.0 * (attempt + 1)
                log.warning("kick %s: HTTP %d, retrying in %.0fs", slug, resp.status_code, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            body: dict[str, Any] = resp.json()
            return body
        log.error("kick %s: giving up after %d attempts", slug, MAX_RETRIES)
        return None

    def _fetch_for_channel(self, slug: str, since: datetime) -> list[Clip]:
        clips: list[Clip] = []
        skipped_game = 0
        cursor: str | None = None
        for _ in range(MAX_PAGES_PER_CHANNEL):
            params = {"sort": "view", "time": "day"}
            if cursor:
                params["cursor"] = cursor
            body = self._get(slug, params)
            if body is None:
                break
            page = body.get("clips") or []
            for raw in page:
                created = _parse_ts(str(raw["created_at"]))
                if created < since:  # `time=day` is a rolling window, may exceed the lookback
                    continue
                category = str((raw.get("category") or {}).get("name") or "")
                if self._games and not matches_game(category, self._games):
                    skipped_game += 1
                    continue
                clips.append(self._to_clip(slug, raw, created))
            cursor = body.get("nextCursor")
            if not cursor or not page:
                break
        log.info(
            "kick %s: %d clips since %s (%d outside game filter)",
            slug, len(clips), since.isoformat(timespec="seconds"), skipped_game,
        )  # fmt: skip
        return clips

    @staticmethod
    def _to_clip(slug: str, raw: dict[str, Any], created: datetime) -> Clip:
        clip_id = str(raw["id"])
        category = raw.get("category") or {}
        return Clip(
            id=clip_id,
            platform=Platform.KICK,
            broadcaster_login=slug,
            broadcaster_name=str((raw.get("channel") or {}).get("username") or slug),
            title=str(raw.get("title") or "").strip(),
            url=CLIP_PAGE_URL.format(slug=slug, clip_id=clip_id),
            view_count=int(raw.get("views") or raw.get("view_count") or 0),
            created_at=created,
            duration_s=float(raw.get("duration") or 0),
            vod_id=str(raw.get("livestream_id") or "") or None,
            vod_offset_s=None,
            game_id=str(category.get("id") or raw.get("category_id") or "") or None,
        )
