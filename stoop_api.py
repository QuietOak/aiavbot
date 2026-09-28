# AIAVBOT - a Discord bot for the Dreamers AI Hub community
# Copyright (C) 2026 Oak
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU Affero General Public License v3.0 or later. See LICENSE.
"""
stoop_api.py - Read-only client for The Stoop (FrontPorch AI) partner API.

The key goes in the bot's env file as STOOP_API_KEY (never in the repo, never in chat).
It is sent as "Authorization: Bearer ..." and is never logged, shown or put in error messages.

What the API gives us (confirmed by the developer):
  GET /partner/characters?sort=newest&rating=sfw|nsfw|all&since=<ISO>&take<=48&page=N
      -> {"total", "page", "take", "items": [card, ...]}
      `since` is inclusive and matches createdAt OR updatedAt, so it doubles as a change feed.
      Keep paging (same since/sort/rating) until a page is shorter than `take`.
  GET /partner/characters/{id}     -> card (404 while missing, in review, rejected, removed)
  GET /partner/assets/{id}/raw     -> image bytes; ?v=thumb = WebP, <= 512 px
  GET /partner/stats               -> totals ({"cards": {"total", "nsfw"}, ...})

Our keys have the 18+ ceiling: the LIST defaults to SFW, but DETAIL and IMAGES return 18+ cards.
So every caller must check card["nsfw"] before showing a card anywhere.

Limits: 60 requests/min per key. A 429 has no Retry-After: we pause every call for 60 s.
This client also caps itself at MAX_PER_MINUTE so we never get near the limit.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import aiohttp

log = logging.getLogger("aiavbot.stoop")

DEFAULT_BASE = "https://api.frontporchai.app"
HUB_CARD_URL = "https://hub.frontporchai.app/card/{id}"
USER_AGENT = "AIAVBot/1.0 (+https://github.com/QuietOak/aiavbot)"
MAX_TAKE = 48
MAX_PAGES = 60                 # per poll; 60 x 48 = 2880 changed cards
MAX_PER_MINUTE = 40            # our own cap (the API allows 60)
RATE_LIMIT_PAUSE = 60          # seconds to pause after a 429
TIMEOUT = aiohttp.ClientTimeout(total=20)
MAX_IMAGE_BYTES = 8 * 1024 * 1024

UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
CARD_LINK_RE = re.compile(rf"https?://(?:www\.)?hub\.frontporchai\.app/card/({UUID_RE})", re.I)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class StoopError(Exception):
    """Any API problem. `status` is the HTTP status (None for network trouble)."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class StoopUnauthorized(StoopError):
    pass


class StoopRateLimited(StoopError):
    pass


class StoopDisabled(StoopError):
    pass


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def card_url(card_id: str) -> str:
    return HUB_CARD_URL.format(id=card_id)


def card_id_from_url(url: str) -> Optional[str]:
    m = CARD_LINK_RE.search(url or "")
    return m.group(1).lower() if m else None


def iso(dt: datetime) -> str:
    """UTC ISO-8601 with milliseconds and Z, the same format the API uses."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def now_iso() -> str:
    return iso(datetime.now(timezone.utc))


def parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def changed_at(card: dict) -> str:
    """The later of createdAt / updatedAt (what `since` matches on)."""
    return max(card.get("createdAt") or "", card.get("updatedAt") or "")


def creator_name(card: dict) -> Optional[str]:
    c = card.get("creator")
    if not isinstance(c, dict):
        return None
    return (c.get("displayName") or "").strip() or None


def original_creator_name(card: dict) -> Optional[str]:
    oc = card.get("originalCreator")
    if isinstance(oc, dict):
        return (oc.get("displayName") or oc.get("name") or "").strip() or None
    if isinstance(oc, str):
        return oc.strip() or None
    return None


def to_character_info(card: dict) -> dict:
    """An API card as the character dict the rest of the bot uses (see character_links)."""
    return {
        "id": card.get("id"),
        "url": card_url(card["id"]) if card.get("id") else None,
        "site": "The Stoop",
        "title": (card.get("name") or "").strip() or None,
        "description": (card.get("summary") or "").strip() or None,
        "creator": creator_name(card),
        "tags": [str(t) for t in (card.get("tags") or []) if t][:15],
        "image_url": None,                       # the art needs the key: download and attach instead
        "nsfw": bool(card.get("nsfw")),
        "source": "character",
        "stoop": True,
        "stoop_asset": card.get("primaryAssetId"),
    }


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

class StoopClient:
    def __init__(self, key: Optional[str], base: Optional[str] = None,
                 session: Optional[aiohttp.ClientSession] = None,
                 max_per_minute: int = MAX_PER_MINUTE, pause_seconds: float = RATE_LIMIT_PAUSE):
        self._key = (key or "").strip() or None
        self.base = (base or DEFAULT_BASE).rstrip("/")
        self._session = session
        self._own_session = session is None
        self.max_per_minute = max_per_minute
        self.pause_seconds = pause_seconds
        self._sent: deque[float] = deque()
        self._paused_until = 0.0
        self._lock = asyncio.Lock()
        # Health, for /aiav status
        self.last_ok: Optional[float] = None
        self.last_error: Optional[str] = None
        self.last_error_at = 0.0
        self.unauthorized = False
        self.requests_this_hour: deque[float] = deque()

    @property
    def enabled(self) -> bool:
        return self._key is not None

    def health(self) -> str:
        if not self.enabled:
            return "not configured"
        if self.unauthorized:
            return "key rejected (401)"
        if time.monotonic() < self._paused_until:
            return "rate-limited, pausing"
        if self.last_error and (self.last_ok is None or self.last_error_at > self.last_ok):
            return f"error: {self.last_error}"
        return "ok" if self.last_ok else "not used yet"

    def requests_last_hour(self) -> int:
        cutoff = time.monotonic() - 3600
        while self.requests_this_hour and self.requests_this_hour[0] < cutoff:
            self.requests_this_hour.popleft()
        return len(self.requests_this_hour)

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    def _redact(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    async def _wait_turn(self) -> None:
        """Respect the 60 s pause after a 429 and our own per-minute cap."""
        async with self._lock:
            while True:
                now = time.monotonic()
                if now < self._paused_until:
                    await asyncio.sleep(self._paused_until - now)
                    continue
                while self._sent and now - self._sent[0] >= 60:
                    self._sent.popleft()
                if len(self._sent) < self.max_per_minute:
                    self._sent.append(now)
                    self.requests_this_hour.append(now)
                    return
                await asyncio.sleep(60 - (now - self._sent[0]) + 0.05)

    async def _get(self, path: str, params: Optional[dict] = None, *, want: str = "json"):
        """GET a path. Returns parsed JSON (want='json') or (bytes, content_type) (want='bytes').
        Returns None on 404. Raises StoopError for everything else that isn't a 200."""
        if not self.enabled:
            raise StoopDisabled("STOOP_API_KEY is not set")
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT)
        await self._wait_turn()
        headers = {"Authorization": f"Bearer {self._key}", "User-Agent": USER_AGENT,
                   "Accept": "application/json" if want == "json" else "image/*"}
        try:
            async with self._session.get(self.base + path, params=params, headers=headers) as resp:
                status = resp.status
                if status == 200:
                    if want == "json":
                        try:
                            data = await resp.json(content_type=None)
                        except ValueError:
                            self._note_error("bad JSON")
                            raise StoopError(f"unreadable reply on {path}", status) from None
                    else:
                        body = bytearray()
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            body.extend(chunk)
                            if len(body) > MAX_IMAGE_BYTES:
                                raise StoopError("image larger than 8 MB", status)
                        data = (bytes(body), resp.headers.get("Content-Type", "image/png"))
                    self.last_ok = time.monotonic()
                    self.unauthorized = False
                    return data
                text = (await resp.text(errors="replace"))[:200]
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self._note_error(f"network: {type(e).__name__}")
            raise StoopError(self._redact(f"network error on {path}: {type(e).__name__}")) from None

        if status == 404:
            self.last_ok = time.monotonic()      # a clean "not found" is a healthy answer
            return None
        if status == 401:
            self.unauthorized = True
            self._note_error("401 unauthorized")
            raise StoopUnauthorized("The Stoop rejected the key (401). It may be wrong or revoked.", 401)
        if status == 429:
            self._paused_until = time.monotonic() + self.pause_seconds
            self._note_error("429 rate limited")
            log.warning("The Stoop API rate limit hit; pausing all calls for %ds", self.pause_seconds)
            raise StoopRateLimited("rate limited", 429)
        self._note_error(f"HTTP {status}")
        raise StoopError(self._redact(f"HTTP {status} on {path}: {text}"), status)

    def _note_error(self, text: str) -> None:
        self.last_error = text
        self.last_error_at = time.monotonic()

    # ------------------------------------------------------------ endpoints
    async def list_page(self, *, rating: str, since: Optional[str] = None, page: int = 0,
                        take: int = MAX_TAKE, sort: str = "newest") -> dict:
        if rating not in ("sfw", "nsfw", "all"):
            raise ValueError("rating must be sfw, nsfw or all")     # never rely on the API default
        params = {"sort": sort, "rating": rating, "take": str(take), "page": str(page)}
        if since:
            params["since"] = since
        data = await self._get("/partner/characters", params)
        if not isinstance(data, dict):
            raise StoopError("unexpected list reply")
        return data

    async def changes_since(self, *, rating: str, since: str,
                            max_pages: int = MAX_PAGES) -> tuple[list[dict], bool]:
        """Every card created or updated at/after `since`, deduped by id.
        Returns (cards, complete). complete=False if we stopped at max_pages."""
        seen: dict[str, dict] = {}
        for page in range(max_pages):
            data = await self.list_page(rating=rating, since=since, page=page)
            items = data.get("items") or []
            for card in items:
                if isinstance(card, dict) and card.get("id"):
                    seen[card["id"]] = card
            take = data.get("take") or MAX_TAKE
            if len(items) < take:
                return list(seen.values()), True
        log.warning("Stoop %s poll stopped at %d pages; some changes may be skipped", rating, max_pages)
        return list(seen.values()), False

    async def card(self, card_id: str) -> Optional[dict]:
        """One card, or None if it isn't available (missing, in review, rejected, removed)."""
        data = await self._get(f"/partner/characters/{card_id}")
        return data if isinstance(data, dict) and data.get("id") else None

    async def image(self, asset_id: str, *, thumb: bool = True) -> Optional[tuple[bytes, str]]:
        """(bytes, content_type) for an asset, or None if it's gone (a new picture has a new id)."""
        return await self._get(f"/partner/assets/{asset_id}/raw", {"v": "thumb"} if thumb else None,
                               want="bytes")

    async def stats(self) -> Optional[dict]:
        data = await self._get("/partner/stats")
        return data if isinstance(data, dict) else None
