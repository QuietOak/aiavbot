# AIAVBOT - a Discord bot for the Dreamers AI Hub community
# Copyright (C) 2026 Oak
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU Affero General Public License v3.0 or later. See LICENSE.
"""
reactions.py - Reaction tracking and milestone shout-outs.

Watches emoji reactions in the AIAV channels (music, collab requests, gallery, lounge, their threads,
and active theme destinations), or in every channel if /aiav settings milestone_channels:all.

- Counts reactions on "shared work": posts with a link, an image/file, or an embed, plus the bot's
  own song/collab/gallery cards (credited to the member who made them). Counts feed the nightly update.
- When a post's total reactions reach 3, 10, 20, 30 or 50 (texts.MILESTONES), the bot replies to it
  in the same channel. Each milestone is announced once per post. If several are passed at once
  (e.g. the bot was offline), only the highest is announced.

Discord doesn't send reaction totals with each reaction, so the bot reads the post once the
first time it sees a reaction on it, then keeps count itself.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from typing import Optional

import discord
from discord.ext import commands

import texts as T
from storage import GuildConfig, Storage

log = logging.getLogger("aiavbot.reactions")

TRACK_MAX = 5000                    # posts kept in memory
URL_RE = re.compile(r"https?://\S+", re.I)


class Reactions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db: Storage = bot.db  # type: ignore[attr-defined]
        self.tracked: "OrderedDict[int, dict]" = OrderedDict()

    # ------------------------------------------------------------ scope
    def in_scope(self, cfg: GuildConfig, channel_id: int) -> bool:
        if cfg.milestone_scope == "all":
            return True
        ids = {cfg.music_channel_id, cfg.collab_channel_id, cfg.gallery_channel_id, cfg.lounge_channel_id}
        ids |= self.db.theme_target_ids(cfg.guild_id)
        ids.discard(None)
        if channel_id in ids:
            return True
        ch = self.bot.get_channel(channel_id)
        return getattr(ch, "parent_id", None) in ids      # threads inside those channels

    # ------------------------------------------------------------ events
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle(payload, +1)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle(payload, -1)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
        info = self.tracked.get(payload.message_id)
        if info:
            info["count"] = 0

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent) -> None:
        self.tracked.pop(payload.message_id, None)       # re-read next time

    async def _handle(self, payload: discord.RawReactionActionEvent, delta: int) -> None:
        try:
            if payload.guild_id is None or (self.bot.user and payload.user_id == self.bot.user.id):
                return
            cfg = self.db.get_config(payload.guild_id)
            if not cfg or not self.in_scope(cfg, payload.channel_id):
                return

            info = self.tracked.get(payload.message_id)
            if info is None:
                if delta < 0:
                    return                      # never seen this post; nothing to take away from
                info = await self._load(payload)  # fresh read already includes this reaction
                if info is None:
                    return
            else:
                info["count"] = max(0, info["count"] + delta)
                self.tracked.move_to_end(payload.message_id)

            if not info["eligible"]:
                return
            self.db.log(payload.guild_id, None, payload.user_id, "reaction" if delta > 0 else "reaction_removed")
            if delta > 0 and cfg.milestones:
                await self._check_milestone(payload, info)
        except Exception:
            log.exception("Reaction handling failed")

    async def _load(self, payload: discord.RawReactionActionEvent) -> Optional[dict]:
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except discord.HTTPException:
                return None
        try:
            msg = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return None

        author_id: Optional[int] = msg.author.id
        if msg.author.bot:
            owner = self.db.card_owner(msg.id)       # our theme/collab/gallery cards count for their creator
            eligible = owner is not None
            author_id = owner
        else:
            eligible = bool(msg.attachments or msg.embeds or URL_RE.search(msg.content or ""))

        info = {
            "eligible": eligible,
            "count": sum(r.count for r in msg.reactions),
            "author_id": author_id,
            "jump_url": msg.jump_url,
        }
        self.tracked[payload.message_id] = info
        while len(self.tracked) > TRACK_MAX:
            self.tracked.popitem(last=False)
        return info

    async def _check_milestone(self, payload: discord.RawReactionActionEvent, info: dict) -> None:
        reached = [m for m in T.MILESTONES if info["count"] >= m]
        if not reached:
            return
        top = reached[-1]
        if top <= self.db.get_milestone(payload.message_id):
            return
        # Record first (no await in between), so two quick reactions can't both announce.
        self.db.set_milestone(payload.message_id, payload.guild_id, payload.channel_id, info["author_id"], top)

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return
        author = f"<@{info['author_id']}>" if info["author_id"] else "someone"
        text = T.MILESTONE_MESSAGES.get(top, T.MILESTONE_DEFAULT).format(
            author=author, count=max(top, info["count"]), link=info["jump_url"])
        try:
            await channel.get_partial_message(payload.message_id).reply(
                text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("Couldn't post milestone: %s", e)
            return
        self.db.log(payload.guild_id, None, info["author_id"], "milestone")
        log.info("Milestone %d on message %s", top, payload.message_id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reactions(bot))
