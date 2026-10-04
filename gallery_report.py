# AIAVBOT - a Discord bot for the Dreamers AI Hub community
# Copyright (C) 2026 Oak
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU Affero General Public License v3.0 or later. See LICENSE.
"""
gallery_report.py - "Who was involved in what", for /aiav gallery_report.

Reads the AIAV gallery channel for one month and counts, per person, how many publications they were part of:
  - A collab the bot presented (congratulations card): the presenter, every collaborator they picked,
    and every name they typed under "Anyone else?". Each person counts once per collab.
  - Any other post with an image, file, link or embed: counts for the person who posted it.
  - Replies, short chat and the bot's prompts don't count. A post the bot presented is counted once,
    through its card.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Optional

import discord

URL_RE = re.compile(r"https?://\S+", re.I)
SPLIT_RE = re.compile(r"\s*(?:,|;|/|&|\+|\band\b|\n)\s*", re.I)


def month_bounds(month: Optional[str], now: Optional[datetime] = None) -> tuple[datetime, datetime, str]:
    """(start, end, label) for "YYYY-MM", or for the previous month if month is None. Local time."""
    now = now or datetime.now().astimezone()
    if month:
        year, mon = (int(x) for x in month.strip().split("-", 1))
        start = now.replace(year=year, month=mon, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = (first_this - timedelta(days=1)).replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end, start.strftime("%B %Y")


def split_names(text: Optional[str]) -> list[str]:
    """'Nova (@nova on Suno), my friend Sam & Kai' -> ['Nova (@nova on Suno)', 'my friend Sam', 'Kai']."""
    out, seen = [], set()
    for part in SPLIT_RE.split(text or ""):
        name = part.strip(" .")
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def is_publication(msg: discord.Message) -> bool:
    return bool(msg.attachments or msg.embeds or URL_RE.search(msg.content or ""))


async def count_gallery(db, guild: discord.Guild, channel, start: datetime, end: datetime,
                        bot_user_id: Optional[int]) -> tuple[int, Counter, dict]:
    """(publications, Counter of person key -> count, {person key: display name})."""
    counts: Counter = Counter()
    names: dict[str, str] = {}
    publications = 0

    def member_key(uid: int, fallback: Optional[str] = None) -> str:
        key = f"u{uid}"
        if key not in names:
            m = guild.get_member(uid) if guild else None
            names[key] = m.display_name if m else (fallback or f"User {uid}")
        return key

    async for msg in channel.history(limit=None, after=start, before=end, oldest_first=True):
        people: set[str] = set()
        if msg.author.bot:
            if bot_user_id is not None and msg.author.id != bot_user_id:
                continue
            entry = db.gallery_entry(msg.id)
            if not entry:
                continue                                  # a prompt or another bot message
            people.add(member_key(entry["creator_id"]))
            for uid in json.loads(entry["member_ids"] or "[]"):
                people.add(member_key(int(uid)))
            for name in split_names(entry["others"]):
                # A typed name that matches someone already credited is the same person.
                match = next((k for k in people if names.get(k, "").lower() == name.lower()), None)
                if match is None:
                    match = next((k for k, v in names.items() if k.startswith("u") and v.lower() == name.lower()), None)
                key = match or f"n:{name.lower()}"
                names.setdefault(key, name)
                people.add(key)
        else:
            if msg.reference is not None or not is_publication(msg):
                continue
            if db.gallery_entry_for_post(msg.id):
                continue                                  # counted through the bot's card
            people.add(member_key(msg.author.id, msg.author.display_name))
        if people:
            publications += 1
            counts.update(people)
    return publications, counts, names


def ranked(counts: Counter, names: dict) -> list[tuple[str, int]]:
    """[(display name, count)], most involved first, then by name."""
    return [(names.get(k, k), n) for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], names.get(kv[0], "").lower()))]


def build_report_embed(label: str, publications: int, counts: Counter, names: dict,
                       max_rows: int = 40) -> discord.Embed:
    """The pretty version: medals for the top three, then a numbered list."""
    import texts as T
    rows = ranked(counts, names)
    lines = [T.REPORT_SUMMARY.format(entries=publications, s="" if publications == 1 else "s", people=len(rows)), ""]
    for i, (name, n) in enumerate(rows[:max_rows]):
        rank = T.REPORT_MEDALS[i] if i < len(T.REPORT_MEDALS) else f"`{i + 1}.`"
        lines.append(T.REPORT_ROW.format(rank=rank, name=discord.utils.escape_markdown(name), count=n))
    if len(rows) > max_rows:
        lines.append(T.REPORT_MORE.format(n=len(rows) - max_rows))
    embed = discord.Embed(title=T.REPORT_EMBED_TITLE.format(month=label), description="\n".join(lines)[:4000],
                          color=0xF5B942)
    embed.set_footer(text=T.REPORT_FOOTER)
    return embed


def format_report(label: str, publications: int, counts: Counter, names: dict, texts=None) -> list[str]:
    """Plain lines (copy-friendly): 'Name: count', most involved first."""
    return [f"{name}: {n}" for name, n in ranked(counts, names)]
