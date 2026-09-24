# AIAVBOT - a Discord bot for the Dreamers AI Hub community
# Copyright (C) 2026 Oak
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU Affero General Public License v3.0 or later. See LICENSE.
"""
music_links.py - Music links other than Suno (YouTube, Spotify, SoundCloud, Bandcamp, ...).

Suno links get full details from suno_fetch.py. For other sites we get what their public
"oEmbed" preview data or page preview tags offer: usually a title, sometimes the artist/channel,
and a thumbnail or cover. No style or lyrics.

Add or remove sites in MUSIC_SITES.
"""

from __future__ import annotations

import html
import re
from typing import Optional
from urllib.parse import quote, urlparse

import aiohttp

import suno_fetch

# host (or host suffix) -> (display name, oEmbed endpoint or None to read page tags)
MUSIC_SITES: dict[str, tuple[str, Optional[str]]] = {
    "youtube.com": ("YouTube", "https://www.youtube.com/oembed?format=json&url={url}"),
    "youtu.be": ("YouTube", "https://www.youtube.com/oembed?format=json&url={url}"),
    "music.youtube.com": ("YouTube Music", "https://www.youtube.com/oembed?format=json&url={url}"),
    "open.spotify.com": ("Spotify", "https://open.spotify.com/oembed?url={url}"),
    "spotify.link": ("Spotify", None),
    "soundcloud.com": ("SoundCloud", "https://soundcloud.com/oembed?format=json&url={url}"),
    "on.soundcloud.com": ("SoundCloud", None),
    "bandcamp.com": ("Bandcamp", None),
    "music.apple.com": ("Apple Music", None),
    "udio.com": ("Udio", None),
    "tidal.com": ("Tidal", None),
    "listen.tidal.com": ("Tidal", None),
    "deezer.com": ("Deezer", None),
    "audiomack.com": ("Audiomack", None),
}

URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.I)


def site_for(url: str) -> Optional[str]:
    """Display name of the music site for a URL, or None if it isn't a known music site."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    host = host[4:] if host.startswith("www.") else host
    if host in MUSIC_SITES:
        return MUSIC_SITES[host][0]
    for suffix, (name, _) in MUSIC_SITES.items():
        if host.endswith("." + suffix):        # e.g. artist.bandcamp.com, m.youtube.com
            return name
    return None


def find_music_links(text: str) -> list[str]:
    """All Suno links plus links to other known music sites, in order, without duplicates."""
    found: list[tuple[int, str]] = []
    for url in suno_fetch.find_suno_links(text or ""):
        found.append(((text or "").find(url), url))
    suno = {u for _, u in found}
    for m in URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,!?;:>")
        if url in suno or "suno." in url:
            continue
        if site_for(url):
            found.append((m.start(), url))
    seen, out = set(), []
    for _, url in sorted(found):
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def is_suno(url: str) -> bool:
    return bool(suno_fetch.SONG_LINK_RE.search(url) or suno_fetch.SHORT_LINK_RE.search(url))


_META_RE = re.compile(
    r'<meta\s+[^>]*?(?:property|name)=["\']([^"\']+)["\'][^>]*?content=["\']([^"\']*)["\']', re.I)


async def fetch_link(url: str, session: aiohttp.ClientSession) -> dict:
    """Title / artist / image for a non-Suno music link. Never raises; missing info is None."""
    site = site_for(url) or "the web"
    info = {"id": None, "url": url, "site": site, "title": None, "artist": None,
            "style": None, "lyrics": None, "image_url": None, "source": "link"}

    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    endpoint = None
    for key, (_, ep) in MUSIC_SITES.items():
        if host == key or host.endswith("." + key):
            endpoint = ep
            break

    try:
        if endpoint:
            async with session.get(endpoint.format(url=quote(url, safe=""))) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    info["title"] = (data.get("title") or "").strip() or None
                    info["artist"] = (data.get("author_name") or "").strip() or None
                    info["image_url"] = data.get("thumbnail_url") or None
                    if info["title"]:
                        return info
        # Page preview tags (also the fallback when oEmbed fails)
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status != 200:
                return info
            page = await resp.text(errors="replace")
        tags = {}
        for name, content in _META_RE.findall(page[:300_000]):
            tags.setdefault(name.lower(), html.unescape(content))
        info["title"] = info["title"] or (tags.get("og:title") or tags.get("twitter:title") or "").strip() or None
        info["image_url"] = info["image_url"] or tags.get("og:image") or tags.get("twitter:image") or None
        if site == "the web" and tags.get("og:site_name"):
            info["site"] = tags["og:site_name"]
    except Exception as e:  # network trouble: keep whatever we have
        info["error"] = repr(e)
    return info
