# AIAVBOT - a Discord bot for the Dreamers AI Hub community
# Copyright (C) 2026 Oak
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU Affero General Public License v3.0 or later. See LICENSE.
"""
nightly.py - Scheduled posts in the AIAV Club lounge: the activity update (weekly by default, or daily)
and, if switched on, the monthly gallery report.

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


def stats_line(stats: dict, labels: list = None) -> str:
    """'🎵 12 shares · 🤝 3 collab requests · …'. Labels are (key, label) or (key, label, emoji)."""
    parts = []
    for item in (labels or T.STAT_LABELS):
        key, label = item[0], item[1]
        emoji = f"{item[2]} " if len(item) > 2 else ""
        parts.append(f"{emoji}{stats.get(key, 0)} {label}")
    return " · ".join(parts)


def periods_for(cadence: str) -> list:
    return T.PERIODS_DAILY if cadence == "daily" else T.PERIODS_WEEKLY


def period_stats(db: Storage, guild_id: int, end: datetime, cadence: str = "weekly") -> list[tuple[str, dict]]:
    """[(label, stats), ...] for each period of the schedule, ending at `end`."""
    out = []
    for days, label in periods_for(cadence):
        start = end - timedelta(days=days)
        stats = db.stats_between(guild_id, start.timestamp(), end.timestamp())
        stats["reactions"] = max(0, stats.get("reaction", 0) - stats.get("reaction_removed", 0))
        out.append((label, stats))
    return out


def build_update_embed(db: Storage, guild_id: int, end: datetime, scheduled: bool,
                       stoop_total: Optional[int] = None) -> discord.Embed:
    cfg = db.get_config(guild_id)
    cadence = cfg.update_cadence if cfg else "weekly"
    title = (T.NIGHTLY_TITLE if cadence == "daily" else T.WEEKLY_TITLE) if scheduled else T.UPDATE_TITLE
    embed = discord.Embed(title=title, description=T.NIGHTLY_INTRO, color=0x9B7EDE, timestamp=end)
    porch = bool(cfg and (cfg.porch_channel_id or cfg.porch18_channel_id))
    for label, stats in period_stats(db, guild_id, end, cadence):
        value = stats_line(stats)
        if porch:
            value += f"\n{T.PORCH_STAT_TITLE}: {stats_line(stats, T.PORCH_STAT_LABELS)}"
        embed.add_field(name=label, value=value, inline=False)
    if porch and stoop_total:
        embed.set_footer(text=T.PORCH_STOOP_TOTAL.format(total=stoop_total))
    return embed


async def stoop_total(bot: commands.Bot) -> Optional[int]:
    """Total characters on The Stoop (SFW + 18+), if the porch module and API key are set up."""
    porch = bot.get_cog("Porch")
    if porch is None:
        return None
    stats = await porch.stoop_stats()
    cards = (stats or {}).get("cards")
    return cards.get("total") if isinstance(cards, dict) and isinstance(cards.get("total"), int) else None


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
        """Post the activity update in the guild's lounge (nightly=True: the scheduled one)."""
        cfg = self.db.get_config(guild_id)
        if not cfg or not cfg.lounge_channel_id:
            return None
        channel = self.bot.get_channel(cfg.lounge_channel_id)
        if channel is None:
            return None
        end = end or datetime.now().astimezone()
        try:
            total = await stoop_total(self.bot) if (cfg.porch_channel_id or cfg.porch18_channel_id) else None
            return await channel.send(embed=build_update_embed(self.db, guild_id, end, nightly, total))
        except discord.HTTPException as e:
            log.warning("Couldn't post update in lounge: %s", e)
            return None

    async def post_gallery_report(self, guild_id: int, month: Optional[str] = None) -> Optional[discord.Message]:
        """Post the gallery report (default: last month) in the lounge."""
        import gallery_report as G
        cfg = self.db.get_config(guild_id)
        if not cfg or not cfg.lounge_channel_id or not cfg.gallery_channel_id:
            return None
        lounge = self.bot.get_channel(cfg.lounge_channel_id)
        gallery = self.bot.get_channel(cfg.gallery_channel_id)
        guild = getattr(lounge, "guild", None)
        if lounge is None or gallery is None:
            return None
        start, end, label = G.month_bounds(month)
        try:
            pubs, counts, names = await G.count_gallery(self.db, guild, gallery, start, end,
                                                        self.bot.user.id if self.bot.user else None)
            if not pubs:
                return None
            return await lounge.send(T.REPORT_INTRO_AUTO, embed=G.build_report_embed(label, pubs, counts, names),
                                     allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("Couldn't post gallery report: %s", e)
            return None

    @tasks.loop(minutes=1)
    async def check(self) -> None:
        """Scheduled posts at local midnight (caught up until noon if the bot was down):
        the activity update (daily, or weekly on Monday) and the monthly gallery report (on the 1st)."""
        try:
            now = datetime.now().astimezone()
            if now.hour >= CATCH_UP_UNTIL_HOUR:
                return
            today = now.date().isoformat()
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            for cfg in self.db.all_configs():
                if not cfg or not cfg.lounge_channel_id:
                    continue
                await self._activity_update(cfg, now, today, midnight)
                await self._monthly_gallery_report(cfg, now)
        except Exception:
            log.exception("Scheduled update check failed")

    async def _activity_update(self, cfg, now: datetime, today: str, midnight: datetime) -> None:
        if not cfg.nightly_update or cfg.nightly_last == today:
            return
        if cfg.update_cadence != "daily" and now.weekday() != T.WEEKLY_DAY:
            return
        if cfg.nightly_last is None and now.hour > 0:
            # First run ever: don't post a "midnight" update in the middle of the morning.
            self.db.set_nightly(cfg.guild_id, last=today)
            return
        msg = await self.post_update(cfg.guild_id, nightly=True, end=midnight)
        self.db.set_nightly(cfg.guild_id, last=today)   # mark done even if it failed; retry next time
        if msg:
            log.info("Posted %s update in guild %s", cfg.update_cadence, cfg.guild_id)

    async def _monthly_gallery_report(self, cfg, now: datetime) -> None:
        if cfg.gallery_report_cadence != "monthly" or now.day != 1 or not cfg.gallery_channel_id:
            return
        this_month = now.strftime("%Y-%m")
        if cfg.gallery_report_last == this_month:
            return
        self.db.set_schedule(cfg.guild_id, gallery_report_last=this_month)   # once per month, even if it fails
        msg = await self.post_gallery_report(cfg.guild_id)
        if msg:
            log.info("Posted monthly gallery report in guild %s", cfg.guild_id)

    @check.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Nightly(bot))
