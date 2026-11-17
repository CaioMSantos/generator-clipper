"""Kick clip source — EXPERIMENTAL.

Kick's official public API (api.kick.com/public/v1) does not expose clips yet, so this uses the
same internal endpoint the website uses. It sits behind Cloudflare and may return 403 from
datacenter IPs; failures are logged and the cycle continues with the other sources.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from clipper.config import Platform, Streamer
from clipper.models import Clip

log = logging.getLogger(__name__)

CLIPS_URL = "https://kick.com/api/v2/channels/{slug}/clips"
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36",
}


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class KickSource:
    def __init__(self, http: httpx.Client | None = None) -> None:
        self._http = http or httpx.Client(timeout=httpx.Timeout(15.0), headers=HEADERS)

    def fetch_clips(self, streamers: list[Streamer], since: datetime) -> list[Clip]:
        out: list[Clip] = []
        for s in streamers:
            if s.platform != Platform.KICK:
                continue
            try:
                out.extend(self._fetch_for_channel(s.login.lower(), since))
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                log.error("kick %s: failed to fetch clips: %s", s.login, exc)
        return out

    def _fetch_for_channel(self, slug: str, since: datetime) -> list[Clip]:
        resp = self._http.get(CLIPS_URL.format(slug=slug), params={"sort": "date", "time": "day"})
        if resp.status_code == 403:
            log.warning("kick %s: 403 (cloudflare). Rode de um IP residencial ou use proxy.", slug)
            return []
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        clips: list[Clip] = []
        for raw in body.get("clips", []):
            created = _parse_ts(str(raw.get("created_at") or raw["started_at"]))
            if created < since:
                continue
            clip_id = str(raw["id"])
            clips.append(
                Clip(
                    id=clip_id,
                    platform=Platform.KICK,
                    broadcaster_login=slug,
                    broadcaster_name=str((raw.get("channel") or {}).get("username") or slug),
                    title=str(raw.get("title") or ""),
                    url=f"https://kick.com/{slug}?clip={clip_id}",
                    view_count=int(raw.get("view_count") or raw.get("views") or 0),
                    created_at=created,
                    duration_s=float(raw.get("duration") or 0),
                    vod_id=str(raw.get("livestream_id") or "") or None,
                    vod_offset_s=None,
                    game_id=str(raw.get("category_id") or "") or None,
                )
            )
        log.info("kick %s: %d clips", slug, len(clips))
        return clips
