# AIAVBOT - a Discord bot for the Dreamers AI Hub community
# Copyright (C) 2026 Oak
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU Affero General Public License v3.0 or later. See LICENSE.
"""
nightly.py - The midnight update posted in the AIAV Club lounge.

At local midnight (the clock of the PC running the bot) it posts a summary of the last day,
7 days and 30 days, counted up to that midnight. If the PC was off or asleep at midnight,
the update is posted when the bot comes back, as long as it's before noon. Each day posts once.

Mods can turn it off with /aiav settings nightly_update:False, or post one now with /aiav update.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord.ext import commands, tasks

import texts as T
from storage import Storage

log = logging.getLogger("aiavbot.nightly")

CATCH_UP_UNTIL_HOUR = 12   # a missed midnight update is still posted until noon


def stats_line(stats: dict) -> str:
    return " · ".join(f"{stats.get(key, 0)} {label}" for key, label in T.STAT_LABELS)


def period_stats(db: Storage, guild_id: int, end: datetime) -> list[tuple[str, dict]]:
    """[(label, stats), ...] for each period in T.NIGHTLY_PERIODS, ending at `end`."""
    out = []
    for days, label in T.NIGHTLY_PERIODS:
        start = end - timedelta(days=days)
        stats = db.stats_between(guild_id, start.timestamp(), end.timestamp())
        stats["reactions"] = max(0, stats.get("reaction", 0) - stats.get("reaction_removed", 0))
        out.append((label, stats))
    return out


def build_update_embed(db: Storage, guild_id: int, end: datetime, nightly: bool) -> discord.Embed:
    embed = discord.Embed(title=T.NIGHTLY_TITLE if nightly else T.UPDATE_TITLE,
                          description=T.NIGHTLY_INTRO, color=0x9B7EDE, timestamp=end)
    for label, stats in period_stats(db, guild_id, end):
        embed.add_field(name=label, value=stats_line(stats), inline=False)
    return embed


class Nightly(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db: Storage = bot.db  # type: ignore[attr-defined]

    async def cog_load(self) -> None:
        self.check.start()

    async def cog_unload(self) -> None:
        self.check.cancel()

    async def post_update(self, guild_id: int, *, nightly: bool,
                          end: Optional[datetime] = None) -> Optional[discord.Message]:
        """Post the update in the guild's lounge. Returns the message, or None if it couldn't."""
        cfg = self.db.get_config(guild_id)
        if not cfg or not cfg.lounge_channel_id:
            return None
        channel = self.bot.get_channel(cfg.lounge_channel_id)
        if channel is None:
            return None
        end = end or datetime.now().astimezone()
        try:
            return await channel.send(embed=build_update_embed(self.db, guild_id, end, nightly))
        except discord.HTTPException as e:
            log.warning("Couldn't post update in lounge: %s", e)
            return None

    @tasks.loop(minutes=1)
    async def check(self) -> None:
        try:
            now = datetime.now().astimezone()
            if now.hour >= CATCH_UP_UNTIL_HOUR:
                return
            today = now.date().isoformat()
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            for cfg in self.db.all_configs():
                if not cfg or not cfg.nightly_update or not cfg.lounge_channel_id:
                    continue
                if cfg.nightly_last == today:
                    continue
                if cfg.nightly_last is None and now.hour > 0:
                    # First run ever: don't post a "nightly" update in the middle of the morning.
                    self.db.set_nightly(cfg.guild_id, last=today)
                    continue
                msg = await self.post_update(cfg.guild_id, nightly=True, end=midnight)
                self.db.set_nightly(cfg.guild_id, last=today)   # mark done even if it failed; retry tomorrow
                if msg:
                    log.info("Posted nightly update in guild %s", cfg.guild_id)
        except Exception:
            log.exception("Nightly update check failed")

    @check.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Nightly(bot))
