# AIAVBOT - a Discord bot for the Dreamers AI Hub community
# Copyright (C) 2026 Oak
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU Affero General Public License v3.0 or later. See LICENSE.
"""
suno_fetch.py - Pull song info from Suno links.

Step 1 of the AIAV bot: given a Suno link (or a whole Discord message that
contains one or more links), return the song's title, creator, style, lyrics,
cover image and a few stats.

How it works
------------
1. Find Suno links in text (short /s/ links and full /song/<id> links).
2. Short links are followed to get the song id.
3. Song data comes from Suno's public clip endpoint (the same JSON the song page
   uses). If that fails, we fall back to the basic tags on the song page.

Note: the clip endpoint is not an official, documented API. Suno can change it
without warning, so the page fallback is there to keep the bot partly working
if that happens.

Written with aiohttp (async) because discord.py already uses it, so this module
can be dropped straight into the bot later.

Usage (command line):
    python suno_fetch.py https://suno.com/s/gyFtd8xYQJfoo0z7
    python suno_fetch.py https://suno.com/s/gyFtd8xYQJfoo0z7 --json
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Optional

import aiohttp

# --------------------------------------------------------------------------- #
# Link detection
# --------------------------------------------------------------------------- #

UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# Full song links: suno.com/song/<uuid>, app.suno.ai/song/<uuid>, suno.com/embed/<uuid>
SONG_LINK_RE = re.compile(
    rf"https?://(?:www\.|app\.)?suno\.(?:com|ai)/(?:song|embed)/({UUID_RE})", re.I
)
# Short share links: suno.com/s/<code>
SHORT_LINK_RE = re.compile(r"https?://(?:www\.)?suno\.com/s/([A-Za-z0-9_-]+)", re.I)

CLIP_API = "https://studio-api.prod.suno.com/api/clip/{id}"
SONG_PAGE = "https://suno.com/song/{id}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = aiohttp.ClientTimeout(total=15)


def find_suno_links(text: str) -> list[str]:
    """Return every Suno link in a block of text, in order, without duplicates."""
    found: list[tuple[int, str]] = []
    for rx in (SONG_LINK_RE, SHORT_LINK_RE):
        for m in rx.finditer(text or ""):
            found.append((m.start(), m.group(0)))
    seen, out = set(), []
    for _, url in sorted(found):
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


# --------------------------------------------------------------------------- #
# Result object
# --------------------------------------------------------------------------- #

@dataclass
class SunoSong:
    id: str
    url: str
    title: Optional[str] = None
    artist: Optional[str] = None          # display name
    handle: Optional[str] = None          # @handle
    style: Optional[str] = None           # the "style of music" / tags text
    lyrics: Optional[str] = None
    description: Optional[str] = None     # creator's caption, or the prompt used in simple mode
    image_url: Optional[str] = None
    video_url: Optional[str] = None
    duration_sec: Optional[float] = None
    instrumental: Optional[bool] = None
    model: Optional[str] = None
    created_at: Optional[str] = None
    plays: Optional[int] = None
    likes: Optional[int] = None
    comments: Optional[int] = None
    explicit: Optional[bool] = None
    is_remix: Optional[bool] = None
    source: str = "clip_api"              # "clip_api" or "page_fallback"
    warnings: list[str] = field(default_factory=list)

    @property
    def duration_text(self) -> Optional[str]:
        if self.duration_sec is None:
            return None
        m, s = divmod(int(round(self.duration_sec)), 60)
        return f"{m}:{s:02d}"

    @property
    def profile_url(self) -> Optional[str]:
        return f"https://suno.com/@{self.handle}" if self.handle else None


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _clean(value) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def parse_clip_json(data: dict, url: str) -> SunoSong:
    """Turn the clip endpoint's JSON into a SunoSong. Kept separate so it can be tested offline."""
    meta = data.get("metadata") or {}
    instrumental = meta.get("make_instrumental")

    lyrics = _clean(meta.get("prompt"))
    if instrumental and lyrics is None:
        lyrics = None  # nothing to show, that's expected

    # Simple-mode songs keep the user's plain-English idea here.
    description = _clean(data.get("caption")) or _clean(meta.get("gpt_description_prompt"))

    audio = data.get("audio_url") or ""
    warnings = []
    if not data.get("is_public", True):
        warnings.append("Song is not public.")
    if "forbidden" in audio:
        # Normal: Suno hides the direct audio file from anonymous requests.
        pass

    return SunoSong(
        id=data.get("id") or "",
        url=url,
        title=_clean(data.get("title")),
        artist=_clean(data.get("display_name")),
        handle=_clean(data.get("handle")),
        style=_clean(meta.get("tags")),
        lyrics=lyrics,
        description=description,
        image_url=_clean(data.get("image_large_url")) or _clean(data.get("image_url")),
        video_url=_clean(data.get("video_url")),
        duration_sec=meta.get("duration"),
        instrumental=instrumental,
        model=_clean(data.get("model_name")) or _clean(data.get("major_model_version")),
        created_at=_clean(data.get("created_at")),
        plays=data.get("play_count"),
        likes=data.get("upvote_count"),
        comments=data.get("comment_count"),
        explicit=data.get("explicit"),
        is_remix=meta.get("is_remix"),
        warnings=warnings,
    )


_META_RE = re.compile(
    r'<meta\s+[^>]*?(?:property|name)=["\']([^"\']+)["\'][^>]*?content=["\']([^"\']*)["\']', re.I
)


def parse_song_page(page_html: str, song_id: str, url: str) -> SunoSong:
    """Fallback: read the basic <meta> tags from the song page."""
    tags = {}
    for name, content in _META_RE.findall(page_html):
        tags.setdefault(name.lower(), html.unescape(content))

    song = SunoSong(id=song_id, url=url, source="page_fallback")
    song.title = _clean(tags.get("og:title"))
    song.image_url = _clean(tags.get("og:image"))
    song.video_url = _clean(tags.get("og:video"))
    song.warnings.append("Clip data unavailable - only basic page info (no style or lyrics).")
    return song


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

async def resolve_song_id(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    """Get the song UUID from any Suno link. Short links are followed."""
    m = SONG_LINK_RE.search(url)
    if m:
        return m.group(1).lower()

    async with session.get(url, allow_redirects=True) as resp:
        final = str(resp.url)
        m = re.search(rf"/song/({UUID_RE})", final)
        if m:
            return m.group(1).lower()
        body = await resp.text(errors="replace")

    # Redirect didn't land on /song/ - look for it in the page instead.
    for rx in (
        rf'property=["\']og:url["\'][^>]*content=["\'][^"\']*/song/({UUID_RE})',
        rf'rel=["\']canonical["\'][^>]*href=["\'][^"\']*/song/({UUID_RE})',
        rf"/song/({UUID_RE})",
    ):
        m = re.search(rx, body, re.I)
        if m:
            return m.group(1).lower()
    return None


async def fetch_song(url: str, session: Optional[aiohttp.ClientSession] = None) -> SunoSong:
    """Fetch info for one Suno link. Raises ValueError if the link can't be resolved."""
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(headers=HEADERS, timeout=TIMEOUT)
    try:
        song_id = await resolve_song_id(session, url)
        if not song_id:
            raise ValueError(f"Couldn't find a song id for {url}")
        song_url = SONG_PAGE.format(id=song_id)

        # Main path: clip JSON
        try:
            async with session.get(CLIP_API.format(id=song_id)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if isinstance(data, dict) and data.get("id"):
                        return parse_clip_json(data, song_url)
                clip_error = f"clip endpoint returned HTTP {resp.status}"
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as e:
            clip_error = f"clip endpoint failed: {e!r}"

        # Fallback: song page meta tags
        async with session.get(song_url) as resp:
            page = await resp.text(errors="replace")
        song = parse_song_page(page, song_id, song_url)
        song.warnings.append(clip_error)
        return song
    finally:
        if own_session:
            await session.close()


async def fetch_all(text: str) -> list[SunoSong | Exception]:
    """Find every Suno link in some text and fetch them all. Errors are returned, not raised."""
    links = find_suno_links(text)
    async with aiohttp.ClientSession(headers=HEADERS, timeout=TIMEOUT) as session:
        return await asyncio.gather(*(fetch_song(u, session) for u in links), return_exceptions=True)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def format_song(song: SunoSong, lyric_lines: int = 12) -> str:
    lines = [f"Title:     {song.title}"]
    by = song.artist or "?"
    if song.handle:
        by += f" (@{song.handle})"
    lines.append(f"By:        {by}")
    lines.append(f"Link:      {song.url}")
    lines.append(f"Style:     {song.style}")
    if song.description:
        lines.append(f"Caption:   {song.description}")
    lines.append(f"Length:    {song.duration_text}    Instrumental: {song.instrumental}    Model: {song.model}")
    lines.append(f"Stats:     {song.plays} plays, {song.likes} likes, {song.comments} comments")
    lines.append(f"Created:   {song.created_at}")
    lines.append(f"Cover:     {song.image_url}")
    lines.append(f"Video:     {song.video_url}")
    if song.lyrics:
        lyr = song.lyrics.splitlines()
        lines.append("Lyrics:")
        lines += ["   " + l for l in lyr[:lyric_lines]]
        if len(lyr) > lyric_lines:
            lines.append(f"   ... ({len(lyr) - lyric_lines} more lines)")
    else:
        lines.append("Lyrics:    (none)")
    for w in song.warnings:
        lines.append(f"Note:      {w}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch song info from Suno links.")
    ap.add_argument("text", nargs="+", help="Suno link(s), or any text containing them")
    ap.add_argument("--json", action="store_true", help="print raw JSON instead of a summary")
    args = ap.parse_args()

    text = " ".join(args.text)
    if not find_suno_links(text):
        print("No Suno links found.")
        return 1

    results = asyncio.run(fetch_all(text))
    rc = 0
    for r in results:
        if isinstance(r, Exception):
            print(f"ERROR: {r}\n")
            rc = 2
        elif args.json:
            print(json.dumps(asdict(r), indent=2, ensure_ascii=False))
        else:
            print(format_song(r) + "\n")
    return rc


if __name__ == "__main__":
    if sys.platform == "win32":
        # Avoid the noisy "Event loop is closed" message on Windows.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main())
