# AIAVBOT - a Discord bot for the Dreamers AI Hub community
# Copyright (C) 2026 Oak
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU Affero General Public License v3.0 or later. See LICENSE.
"""
character_links.py - Recognise shared AI characters and read their basic info.

Two kinds of character shares:
1. Links to character pages (The Stoop, Chub, JanitorAI, ...). We read the page's public
   link-preview tags (og:title / og:description / og:image), the same data Discord shows in its
   own preview. The Stoop's API and search are closed to bots (robots.txt), so we use only card pages.
2. Character card files (PNG or JSON) in the V1/V2/V3 format used by SillyTavern, Chub, RisuAI,
   FrontPorch AI and others. The character data sits inside the file, so we read name, creator notes,
   tags and creator straight from it.

Every result is a dict with "source": "character", so the rest of the bot can tell it apart
from songs. Add or remove sites in CHARACTER_SITES.
"""

from __future__ import annotations

import base64
import html
import json
import re
import struct
from typing import Optional
from urllib.parse import urlparse

import aiohttp

# host -> (display name, regex the URL path must match to be a character page)
CHARACTER_SITES: dict[str, tuple[str, str]] = {
    "hub.frontporchai.app": ("The Stoop", r"^/card/[0-9a-fA-F-]{8,}"),
    "chub.ai": ("Chub", r"^/characters/"),
    "venus.chub.ai": ("Chub", r"^/characters/"),
    "characterhub.org": ("Chub", r"^/characters/"),
    "janitorai.com": ("JanitorAI", r"^/characters/"),
    "character.ai": ("Character.AI", r"^/(character|chat)/"),
    "realm.risuai.net": ("RisuAI Realm", r"^/character/"),
    "pygmalion.chat": ("Pygmalion", r"^/character/"),
    "spicychat.ai": ("SpicyChat", r"^/chatbot/"),
}

URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.I)
NSFW_TAGS = {"nsfw", "18+", "explicit", "adult", "smut", "lewd"}
MAX_CARD_BYTES = 10 * 1024 * 1024


def _host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def site_for(url: str) -> Optional[str]:
    """Display name if the URL is a character page on a known site, else None."""
    host = _host(url)
    entry = CHARACTER_SITES.get(host)
    if not entry:
        return None
    name, path_re = entry
    try:
        path = urlparse(url).path or "/"
    except ValueError:
        return None
    return name if re.search(path_re, path) else None


def find_character_links(text: str) -> list[str]:
    out, seen = [], set()
    for m in URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,!?;:>")
        if url not in seen and site_for(url):
            seen.add(url)
            out.append(url)
    return out


def is_character(info: Optional[dict]) -> bool:
    return bool(info) and info.get("source") == "character"


# --------------------------------------------------------------------------- #
# Character pages (link previews)
# --------------------------------------------------------------------------- #

_META_RE = re.compile(
    r'<meta\s+[^>]*?(?:property|name)=["\']([^"\']+)["\'][^>]*?content=["\']([^"\']*)["\']', re.I)


def _clean_name(title: str, site: str) -> str:
    """'Misty — The Stoop' -> 'Misty'. Only strips a trailing ' — site' style suffix."""
    title = (title or "").strip()
    parts = re.split(r"\s+[—|–-]\s+", title)
    if len(parts) > 1:
        tail = parts[-1].lower()
        if any(w in tail for w in (site.lower(), "stoop", "chub", "janitor", "character", "risu", "pygmalion", "spicy")):
            return " — ".join(parts[:-1]).strip() or title
    return title


def _blank(url: str, site: str) -> dict:
    return {"id": None, "url": url, "site": site, "title": None, "description": None, "creator": None,
            "tags": [], "image_url": None, "nsfw": False, "source": "character"}


async def fetch_character(url: str, session: aiohttp.ClientSession) -> dict:
    """Name / description / image for a character page. Never raises; missing info is None."""
    site = site_for(url) or "the web"
    info = _blank(url, site)
    try:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status != 200:
                info["error"] = f"HTTP {resp.status}"
                return info
            page = await resp.text(errors="replace")
    except Exception as e:
        info["error"] = repr(e)
        return info
    tags = {}
    for name, content in _META_RE.findall(page[:400_000]):
        tags.setdefault(name.lower(), html.unescape(content))
    title = tags.get("og:title") or tags.get("twitter:title") or ""
    info["title"] = _clean_name(title, site) or None
    info["description"] = (tags.get("og:description") or tags.get("twitter:description") or "").strip() or None
    info["image_url"] = tags.get("og:image") or tags.get("twitter:image") or None
    return info


# --------------------------------------------------------------------------- #
# Character card files (PNG / JSON)
# --------------------------------------------------------------------------- #

def _png_text_chunks(data: bytes) -> dict[str, str]:
    """tEXt / iTXt (uncompressed) chunks of a PNG, keyword -> text."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return {}
    out, pos = {}, 8
    while pos + 8 <= len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"tEXt" and b"\x00" in body:
            key, text = body.split(b"\x00", 1)
            out[key.decode("latin-1").lower()] = text.decode("latin-1")
        elif ctype == b"iTXt" and b"\x00" in body:
            key, rest = body.split(b"\x00", 1)
            if len(rest) >= 2 and rest[0] == 0:            # not compressed
                rest = rest[2:]
                _lang, rest = rest.split(b"\x00", 1)
                _tkey, text = rest.split(b"\x00", 1)
                out[key.decode("latin-1").lower()] = text.decode("utf-8", "replace")
        elif ctype == b"IEND":
            break
    return out


def _card_from_json(obj: dict) -> Optional[dict]:
    """V1 (flat), V2 (chara_card_v2) or V3 (chara_card_v3) card JSON -> our dict."""
    if not isinstance(obj, dict):
        return None
    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    name = (data.get("name") or data.get("char_name") or "").strip()
    if not name:
        return None
    tags = [str(t) for t in (data.get("tags") or []) if t][:15]
    # creator_notes is the public blurb meant for sharing; description can be long and private-ish.
    blurb = (data.get("creator_notes") or data.get("description") or data.get("char_persona") or "").strip()
    info = _blank(None, "Character card")
    info.update(title=name, description=blurb or None, creator=(data.get("creator") or "").strip() or None,
                tags=tags, nsfw=any(t.strip().lower() in NSFW_TAGS for t in tags))
    return info


def parse_card_file(data: bytes, filename: str = "") -> Optional[dict]:
    """Character info from a PNG or JSON card file, or None if it isn't a character card."""
    if not data or len(data) > MAX_CARD_BYTES:
        return None
    raw = None
    if data.startswith(b"\x89PNG"):
        chunks = _png_text_chunks(data)
        raw = chunks.get("ccv3") or chunks.get("chara")
        if not raw:
            return None
        try:
            raw = base64.b64decode(raw).decode("utf-8", "replace")
        except Exception:
            return None
    elif filename.lower().endswith(".json") or data.lstrip()[:1] == b"{":
        raw = data.decode("utf-8", "replace")
    if not raw:
        return None
    try:
        return _card_from_json(json.loads(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def looks_like_card_file(filename: str, size: int) -> bool:
    if not isinstance(filename, str) or not isinstance(size, int):
        return False
    name = filename.lower()
    return (name.endswith(".png") or name.endswith(".json")) and 0 < size <= MAX_CARD_BYTES
