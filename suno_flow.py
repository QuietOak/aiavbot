# AIAVBOT - a Discord bot for the Dreamers AI Hub community
# Copyright (C) 2026 Oak
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU Affero General Public License v3.0 or later. See LICENSE.
"""
suno_flow.py - The Suno link flow.

Member posts a Suno link in the music channel
  -> bot replies publicly with a "Share options" button (instant)
  -> Suno info is fetched in the background
  -> poster clicks the button -> private panel:
        Add a line | Post to Collab Requests | Start a thread | Share to <theme> | Just sharing, thanks!

Responsiveness rule: Discord allows 3 seconds to answer a click, and that can't be changed.
So every handler answers (or "defers") FIRST, then does database and network work afterwards.
The only exceptions are buttons that open a pop-up form, because the form itself must be the
first answer; those only do quick database reads before opening it.

All buttons are "dynamic items": their state lives in the button id + database, so they keep
working after the bot restarts.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from dataclasses import asdict
from typing import Optional

import aiohttp
import discord
from discord import ui
from discord.ext import commands, tasks
from discord.utils import escape_markdown

import character_links
import music_links
import suno_fetch
import texts as T
from storage import Season, Share, Storage

log = logging.getLogger("aiavbot.suno")

MAX_SONGS = 5            # max Suno links handled per message
CACHE_TTL = 60 * 60      # seconds to reuse fetched song info
PANEL_COLOR = 0x9B7EDE
GALLERY_COLOR = 0xF5B942
MIN_PROMPT_TEXT = 15     # collab/gallery: ignore text-only messages shorter than this
MAX_IMAGE_BYTES = 8 * 1024 * 1024   # re-upload images up to this size into bot cards


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def short(text: Optional[str], limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def one_line(text: str) -> str:
    return " ".join((text or "").split())


def song_title(share: Share) -> Optional[str]:
    song = share.song
    return song.get("title") if song else None


def channel_url(guild_id: int, channel_id: int, message_id: Optional[int] = None) -> str:
    base = f"https://discord.com/channels/{guild_id}/{channel_id}"
    return f"{base}/{message_id}" if message_id else base


def parse_emoji(text: Optional[str]) -> Optional[discord.PartialEmoji]:
    if not text:
        return None
    try:
        return discord.PartialEmoji.from_str(text)
    except Exception:
        return None


def listen_label(song: dict, url: Optional[str]) -> str:
    if character_links.is_character(song):
        return short(T.CHARACTER_OPEN.format(site=song.get("site") or character_links.site_for(url or "") or "the web"), 80)
    if song.get("site"):
        site = song["site"]
    elif not url or "suno." in url.lower():
        site = "Suno"
    else:
        site = music_links.site_for(url)
    return short(T.COLLAB_LISTEN.format(site=site or "the web"), 80)


def get_flow(interaction: discord.Interaction) -> "SunoFlow":
    return interaction.client.get_cog("SunoFlow")  # type: ignore[return-value]


async def tell(interaction: discord.Interaction, text: str) -> None:
    """Send a private message whether or not the interaction was already answered."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


async def ack(interaction: discord.Interaction, *, thinking: bool = False) -> bool:
    """Answer Discord right away so the 3-second window can't run out while we work.

    If the window was already missed, log it and carry on: the real work (posting a card,
    thread, etc.) doesn't need the interaction."""
    arrived = (discord.utils.utcnow() - interaction.created_at).total_seconds()
    start = time.monotonic()
    try:
        if thinking:
            await interaction.response.defer(ephemeral=True, thinking=True)
        else:
            await interaction.response.defer()
        took = time.monotonic() - start
        if took > 1.5:
            log.warning("Answering Discord took %.1fs (click was %.1fs old when it arrived)", took, arrived)
        return True
    except discord.NotFound:
        log.warning("Reply window expired: click was %.1fs old when it arrived, answering took %.1fs; "
                    "continuing anyway", arrived, time.monotonic() - start)
        return False


def safe(func):
    """Wrap a callback so errors are logged and the member gets a friendly message."""
    @functools.wraps(func)
    async def wrapper(self, interaction: discord.Interaction, *args, **kwargs):
        try:
            return await func(self, interaction, *args, **kwargs)
        except discord.NotFound as e:
            if getattr(e, "code", None) == 10062:
                age = (discord.utils.utcnow() - interaction.created_at).total_seconds()
                log.warning("%s: reply window expired (%.1fs old)", func.__qualname__, age)
            else:
                log.exception("Error in %s", func.__qualname__)
            await tell(interaction, T.GENERIC_ERROR)
        except Exception:
            log.exception("Error in %s", func.__qualname__)
            await tell(interaction, T.GENERIC_ERROR)
    return wrapper


# --------------------------------------------------------------------------- #
# Dynamic (restart-proof) components
# --------------------------------------------------------------------------- #

class OpenPanelButton(ui.DynamicItem[ui.Button], template=r"aiav:open:(?P<post_id>\d+)"):
    """The one button on the public reply. Opens the private panel for the poster."""

    def __init__(self, post_id: int):
        super().__init__(ui.Button(
            label=T.OPEN_BUTTON_LABEL, emoji=T.OPEN_BUTTON_EMOJI,
            style=discord.ButtonStyle.secondary, custom_id=f"aiav:open:{post_id}",
        ))
        self.post_id = post_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["post_id"]))

    @safe
    async def callback(self, interaction: discord.Interaction) -> None:
        await ack(interaction, thinking=True)   # answer first, always
        flow = get_flow(interaction)
        share = flow.db.get_share(self.post_id)
        if not share:
            return await tell(interaction, T.SHARE_GONE)
        if interaction.user.id != share.poster_id:
            return await tell(interaction, T.NOT_YOUR_POST)

        if share.songs is None:
            await flow.get_songs(share, wait=6)
            share = flow.db.get_share(self.post_id)
        embed, view = flow.render_panel(share)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        flow.db.update_share(share.post_id, opened_at=time.time())
        flow.db.log(share.guild_id, share.post_id, interaction.user.id, "open_panel")


class PanelButton(ui.DynamicItem[ui.Button],
                  template=r"aiav:p:(?P<action>line|collab|thread|theme|done):(?P<post_id>\d+)"):
    """Action buttons on the private panel."""

    STYLES = {
        "line": discord.ButtonStyle.primary,
        "collab": discord.ButtonStyle.success,
        "thread": discord.ButtonStyle.primary,
        "theme": discord.ButtonStyle.success,
        "done": discord.ButtonStyle.secondary,
    }

    def __init__(self, action: str, post_id: int, label: str = "…"):
        super().__init__(ui.Button(
            label=short(label, 80), style=self.STYLES[action], custom_id=f"aiav:p:{action}:{post_id}",
        ))
        self.action = action
        self.post_id = post_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["post_id"]))

    @safe
    async def callback(self, interaction: discord.Interaction) -> None:
        # Pop-up buttons can't defer (the pop-up must be the first answer), so only quick reads here.
        flow = get_flow(interaction)
        share = flow.db.get_share(self.post_id)
        if not share:
            return await tell(interaction, T.SHARE_GONE)
        if interaction.user.id != share.poster_id:
            return await tell(interaction, T.NOT_YOUR_POST)

        if self.action == "line":
            await interaction.response.send_modal(LineModal(flow, share))
        elif self.action == "collab":
            cfg = flow.db.get_config(share.guild_id)
            if not cfg or not cfg.collab_channel_id:
                return await tell(interaction, T.COLLAB_NO_CHANNEL)
            await interaction.response.send_modal(CollabModal(flow, share))
        elif self.action == "thread":
            if share.thread_id:
                return await tell(interaction, T.THREAD_DONE.format(
                    url=channel_url(share.guild_id, share.thread_id)))
            await interaction.response.send_modal(ThreadModal(flow, share))
        elif self.action == "theme":
            themes = flow.available_themes(share)
            if not themes:
                return await tell(interaction, T.THEME_ALL_DONE)
            await interaction.response.send_modal(ThemeModal(flow, share, themes))
        elif self.action == "done":
            await flow.just_sharing(interaction, share)


class SongPick(ui.DynamicItem[ui.Select], template=r"aiav:pick:(?P<post_id>\d+)"):
    """Dropdown on the panel when one message has several Suno links."""

    def __init__(self, post_id: int, options: Optional[list[discord.SelectOption]] = None):
        super().__init__(ui.Select(
            custom_id=f"aiav:pick:{post_id}",
            placeholder=T.PICK_SONG_PLACEHOLDER,
            options=options or [discord.SelectOption(label="song", value="0")],
        ), row=0)
        self.post_id = post_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["post_id"]))

    @safe
    async def callback(self, interaction: discord.Interaction) -> None:
        flow = get_flow(interaction)
        share = flow.db.get_share(self.post_id)
        if not share:
            return await tell(interaction, T.SHARE_GONE)
        if interaction.user.id != share.poster_id:
            return await tell(interaction, T.NOT_YOUR_POST)
        idx = int(self.item.values[0]) if self.item.values else 0
        share.selected = idx
        embed, view = flow.render_panel(share)
        await interaction.response.edit_message(embed=embed, view=view)   # answer first
        flow.db.update_share(share.post_id, selected=idx)                  # then save


class PromptButton(ui.DynamicItem[ui.Button],
                   template=r"aiav:(?P<kind>c|g):(?P<action>yes|no):(?P<post_id>\d+)"):
    """Yes/No buttons on the collab-channel and gallery prompts."""

    LABELS = {
        ("c", "yes"): (T.COLLAB_PROMPT_YES, discord.ButtonStyle.success),
        ("c", "no"): (T.COLLAB_PROMPT_NO, discord.ButtonStyle.secondary),
        ("g", "yes"): (T.GALLERY_PROMPT_YES, discord.ButtonStyle.success),
        ("g", "no"): (T.GALLERY_PROMPT_NO, discord.ButtonStyle.secondary),
    }

    def __init__(self, kind: str, action: str, post_id: int, label: Optional[str] = None):
        default_label, style = self.LABELS[(kind, action)]
        label = label or default_label
        super().__init__(ui.Button(label=label, style=style, custom_id=f"aiav:{kind}:{action}:{post_id}"))
        self.kind, self.action, self.post_id = kind, action, post_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["kind"], match["action"], int(match["post_id"]))

    @safe
    async def callback(self, interaction: discord.Interaction) -> None:
        flow = get_flow(interaction)
        share = flow.db.get_share(self.post_id)
        if not share:
            return await tell(interaction, T.SHARE_GONE)
        if interaction.user.id != share.poster_id:
            return await tell(interaction, T.COLLAB_NOT_YOURS if self.kind == "c" else T.GALLERY_NOT_YOURS)

        if self.action == "no":
            await ack(interaction)
            flow.db.update_share(share.post_id, status="dismissed")
            await flow.delete_public_reply(share)
            await tell(interaction, flow.reminder_text(share, self.kind, interaction.user.display_name))
            flow.db.log(share.guild_id, share.post_id, interaction.user.id,
                        "not_request" if self.kind == "c" else "not_collab")
        elif self.kind == "c":
            await interaction.response.send_modal(CollabModal(flow, share))
        else:
            await interaction.response.send_modal(GalleryModal(flow, share))


class InterestedButton(ui.DynamicItem[ui.Button],
                       template=r"aiav:int:(?P<post_id>\d+):(?P<idx>\d+)"):
    """'I'm interested' on a collab request card: opens a thread with the creator."""

    def __init__(self, post_id: int, idx: int):
        super().__init__(ui.Button(
            label=T.COLLAB_INTERESTED, style=discord.ButtonStyle.success,
            custom_id=f"aiav:int:{post_id}:{idx}",
        ))
        self.post_id = post_id
        self.idx = idx

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["post_id"]), int(match["idx"]))

    @safe
    async def callback(self, interaction: discord.Interaction) -> None:
        await ack(interaction, thinking=True)   # answer first, always
        flow = get_flow(interaction)
        row = flow.db.get_collab_for(self.post_id, self.idx)
        if not row:
            return await tell(interaction, T.SHARE_GONE)
        creator_id = row["creator_id"]
        if interaction.user.id == creator_id:
            return await tell(interaction, T.INTEREST_SELF)

        share = flow.db.get_share(self.post_id)
        title = (share.songs[self.idx].get("title") if share and share.songs and self.idx < len(share.songs)
                 else None)
        if not title:
            title = (T.COLLAB_IDEA_TITLE.format(name=share.poster_name)
                     if share and share.kind != "music" else "this song")

        thread = await flow.thread_for_message(
            interaction.message, T.INTEREST_THREAD_NAME.format(title=title))
        if thread is None:
            return await tell(interaction, T.THREAD_FAILED)

        creator = f"<@{creator_id}>"
        if not flow.db.add_interest(row["card_id"], interaction.user.id):
            return await tell(interaction, T.INTEREST_AGAIN.format(url=thread.jump_url))

        await thread.send(
            T.INTEREST_PING.format(interested=interaction.user.mention, creator=creator,
                                   title=escape_markdown(title)),
            allowed_mentions=discord.AllowedMentions(users=[interaction.user, discord.Object(creator_id)]),
        )
        await tell(interaction, T.INTEREST_DONE.format(creator=creator, url=thread.jump_url))
        flow.db.log(row["guild_id"], self.post_id, interaction.user.id, "interested")


# --------------------------------------------------------------------------- #
# Pop-up forms (modals)
# --------------------------------------------------------------------------- #

class _BaseModal(ui.Modal):
    def __init__(self, flow: "SunoFlow", share: Share, title: str):
        super().__init__(title=short(title, 45), timeout=900)
        self.flow = flow
        self.post_id = share.post_id

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Modal error", exc_info=error)
        await tell(interaction, T.GENERIC_ERROR)


class LineModal(_BaseModal):
    def __init__(self, flow: "SunoFlow", share: Share):
        super().__init__(flow, share, T.LINE_MODAL_TITLE)
        self.line = ui.TextInput(
            style=discord.TextStyle.short, placeholder=T.LINE_PLACEHOLDER,
            default=share.line, min_length=2, max_length=T.LINE_MAX, required=True,
        )
        self.add_item(ui.Label(text=T.LINE_LABEL, description=T.LINE_DESCRIPTION, component=self.line))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await ack(interaction)
        db = self.flow.db
        db.update_share(self.post_id, line=one_line(self.line.value), status="kept")
        share = db.get_share(self.post_id)
        await self.flow.update_panel(interaction, share, notice=T.LINE_DONE)
        await self.flow.refresh_public_reply(share)
        db.log(share.guild_id, self.post_id, interaction.user.id, "line")


class CollabModal(_BaseModal):
    def __init__(self, flow: "SunoFlow", share: Share):
        is_char = flow.is_character_share(share)
        super().__init__(flow, share, T.COLLAB_MODAL_TITLE_CHARACTER if is_char else T.COLLAB_MODAL_TITLE)
        type_list = T.CHARACTER_COLLAB_TYPES if is_char else T.COLLAB_TYPES
        self.types = ui.Select(
            options=[discord.SelectOption(label=label, value=value, emoji=emoji, description=desc)
                     for value, label, emoji, desc in type_list],
            min_values=1, max_values=len(type_list), required=True,
        )
        self.add_item(ui.Label(text=T.COLLAB_TYPES_LABEL_CHARACTER if is_char else T.COLLAB_TYPES_LABEL,
                               description=T.COLLAB_TYPES_DESCRIPTION, component=self.types))
        self.note = ui.TextInput(style=discord.TextStyle.paragraph, placeholder=T.COLLAB_NOTE_PLACEHOLDER,
                                 max_length=T.COLLAB_NOTE_MAX, required=False)
        self.add_item(ui.Label(text=T.COLLAB_NOTE_LABEL, component=self.note))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await ack(interaction)
        await self.flow.post_collab_request(
            interaction, self.post_id, list(self.types.values), self.note.value.strip() or None)


class ThreadModal(_BaseModal):
    def __init__(self, flow: "SunoFlow", share: Share):
        super().__init__(flow, share, T.THREAD_MODAL_TITLE)
        self.wants = ui.Select(
            options=[discord.SelectOption(label=label, value=value, emoji=emoji)
                     for value, label, emoji in T.RESPONSE_TYPES],
            min_values=0, max_values=len(T.RESPONSE_TYPES), required=False,
        )
        self.add_item(ui.Label(text=T.RESPONSE_LABEL, description=T.RESPONSE_DESCRIPTION, component=self.wants))
        self.note = ui.TextInput(style=discord.TextStyle.paragraph, placeholder=T.THREAD_NOTE_PLACEHOLDER,
                                 max_length=T.THREAD_NOTE_MAX, required=False)
        self.add_item(ui.Label(text=T.THREAD_NOTE_LABEL, component=self.note))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await ack(interaction)
        flow, db = self.flow, self.flow.db
        share = db.get_share(self.post_id)
        if share.thread_id:
            return await flow.update_panel(interaction, share)

        channel = flow.bot.get_channel(share.channel_id)
        title = song_title(share) or T.THREAD_FALLBACK_TITLE.format(name=share.poster_name)
        thread = None
        if channel is not None:
            thread = await flow.thread_for_message(
                channel.get_partial_message(share.post_id), T.THREAD_NAME.format(title=title))
        if thread is None:
            return await flow.update_panel(interaction, share, notice=T.THREAD_FAILED)

        user = interaction.user
        parts = [T.THREAD_INTRO.format(title=escape_markdown(title), mention=user.mention)]
        wants = [f"{e} {label}" for value, label, e in T.RESPONSE_TYPES if value in self.wants.values]
        if wants:
            parts.append(T.THREAD_WANTS.format(name=escape_markdown(user.display_name), wants=", ".join(wants)))
        note = self.note.value.strip()
        if note:
            parts.append("\n".join(f"> {ln}" for ln in note.splitlines()))
        parts.append(T.THREAD_PROMPTS_HEADER + "\n" + "\n".join(f"• {p}" for p in T.THREAD_PROMPTS))
        await thread.send("\n\n".join(parts), allowed_mentions=discord.AllowedMentions(users=[user]))

        db.update_share(share.post_id, thread_id=thread.id, status="kept")
        share = db.get_share(self.post_id)
        await flow.update_panel(interaction, share, notice=T.THREAD_DONE.format(url=thread.jump_url))
        await flow.refresh_public_reply(share)
        db.log(share.guild_id, share.post_id, user.id, "thread")


class ThemeModal(_BaseModal):
    """Share a copy of the song to a theme's channel, thread or forum."""

    def __init__(self, flow: "SunoFlow", share: Share, themes: list[Season]):
        if len(themes) == 1:
            title = T.THEME_MODAL_TITLE.format(name=themes[0].name)
        else:
            title = T.THEME_MODAL_TITLE_MANY
        super().__init__(flow, share, title)
        self.theme_ids = [t.id for t in themes]

        self.pick: Optional[ui.Select] = None
        if len(themes) > 1:
            self.pick = ui.Select(
                options=[discord.SelectOption(label=short(t.name, 100), value=str(t.id),
                                              description=short(t.description, 100) or None,
                                              emoji=parse_emoji(t.emoji))
                         for t in themes[:25]],
                min_values=1, max_values=1, required=True,
            )
            self.add_item(ui.Label(text=T.THEME_PICK_LABEL, component=self.pick))

        desc = themes[0].description if len(themes) == 1 and themes[0].description else T.THEME_NOTE_DESCRIPTION
        self.note = ui.TextInput(style=discord.TextStyle.paragraph, placeholder=T.THEME_NOTE_PLACEHOLDER,
                                 max_length=T.THEME_NOTE_MAX, required=False)
        self.add_item(ui.Label(text=T.THEME_NOTE_LABEL, description=short(desc, 100), component=self.note))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await ack(interaction)
        season_id = int(self.pick.values[0]) if self.pick and self.pick.values else self.theme_ids[0]
        await self.flow.share_to_theme(interaction, self.post_id, season_id, self.note.value.strip() or None)


class GalleryModal(_BaseModal):
    """'Is this a collab result?' -> who with, title, about. Collaborators can be off-server."""

    def __init__(self, flow: "SunoFlow", share: Share):
        super().__init__(flow, share, T.GALLERY_MODAL_TITLE)
        default_title = song_title(share)
        self.title_in = ui.TextInput(style=discord.TextStyle.short, placeholder=T.GALLERY_TITLE_PLACEHOLDER,
                                     default=short(default_title, 100) if default_title else None,
                                     max_length=100, required=False)
        self.add_item(ui.Label(text=T.GALLERY_TITLE_LABEL, component=self.title_in))
        self.members = ui.UserSelect(min_values=0, max_values=10, required=False)
        self.add_item(ui.Label(text=T.GALLERY_MEMBERS_LABEL, description=T.GALLERY_MEMBERS_DESCRIPTION,
                               component=self.members))
        self.others = ui.TextInput(style=discord.TextStyle.short, placeholder=T.GALLERY_OTHERS_PLACEHOLDER,
                                   max_length=300, required=False)
        self.add_item(ui.Label(text=T.GALLERY_OTHERS_LABEL, description=T.GALLERY_OTHERS_DESCRIPTION,
                               component=self.others))
        self.note = ui.TextInput(style=discord.TextStyle.paragraph, placeholder=T.GALLERY_NOTE_PLACEHOLDER,
                                 max_length=1000, required=False)
        self.add_item(ui.Label(text=T.GALLERY_NOTE_LABEL, component=self.note))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await ack(interaction)
        await self.flow.post_gallery_collab(
            interaction, self.post_id,
            title=self.title_in.value.strip() or None,
            members=list(self.members.values),
            others=one_line(self.others.value) or None,
            note=self.note.value.strip() or None,
        )


# --------------------------------------------------------------------------- #
# The cog
# --------------------------------------------------------------------------- #

class SunoFlow(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db: Storage = bot.db  # type: ignore[attr-defined]
        self.http: Optional[aiohttp.ClientSession] = None
        self.fetch_tasks: dict[int, asyncio.Task] = {}
        self.cache: dict[str, tuple[float, dict]] = {}

    async def cog_load(self) -> None:
        self.http = aiohttp.ClientSession(headers=suno_fetch.HEADERS, timeout=suno_fetch.TIMEOUT)
        self.bot.add_dynamic_items(OpenPanelButton, PanelButton, SongPick, InterestedButton, PromptButton)
        self.housekeeping.start()

    async def cog_unload(self) -> None:
        self.housekeeping.cancel()
        if self.http:
            await self.http.close()

    # ------------------------------------------------------------ listening
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        cfg = self.db.get_config(message.guild.id)
        if not cfg:
            return
        cid = message.channel.id   # a thread has its own id, so thread messages never match
        if cid == cfg.music_channel_id:
            await self.handle_music_post(message)
        elif cid == cfg.collab_channel_id:
            await self.handle_prompt_post(message, "collab")
        elif cid == cfg.gallery_channel_id:
            await self.handle_prompt_post(message, "gallery")

    async def handle_prompt_post(self, message: discord.Message, kind: str) -> None:
        """Collab channel: 'Starting a collab?'  Gallery: 'Is this a collab result?'"""
        if message.reference is not None:          # replies are conversation, not new posts
            return
        text = message.content.strip()
        has_stuff = bool(message.attachments) or "http://" in text or "https://" in text
        if not has_stuff and len(text) < MIN_PROMPT_TEXT:   # "cool!", "thanks" etc.
            return
        if self.db.get_share(message.id):
            return

        # Characters first (links to character pages, or a PNG/JSON character card file), then music.
        char_links = character_links.find_character_links(text)
        card_info = None
        if kind == "collab" and not char_links:
            card_info = await self.read_card_file(message)
        links = [] if card_info else (char_links + music_links.find_music_links(text))[:MAX_SONGS]
        is_char = bool(char_links or card_info)

        name = message.author.display_name
        self.db.create_share(message.id, message.guild.id, message.channel.id,
                             message.author.id, name, links, kind=kind)
        if card_info:
            self.db.update_share(message.id, songs=[card_info])
        share = self.db.get_share(message.id)
        if links:
            self.start_fetch(share)

        view = ui.View(timeout=None)
        if kind == "collab":
            view.add_item(PromptButton("c", "yes", message.id,
                                       label=T.COLLAB_PROMPT_CHARACTER_YES if is_char else None))
            view.add_item(PromptButton("c", "no", message.id))
            content = (T.COLLAB_PROMPT_CHARACTER if is_char else T.COLLAB_PROMPT).format(name=escape_markdown(name))
        else:
            view.add_item(PromptButton("g", "yes", message.id))
            view.add_item(PromptButton("g", "no", message.id))
            content = T.GALLERY_PROMPT.format(name=escape_markdown(name))
        try:
            reply = await message.reply(content, view=view, mention_author=False,
                                        allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("Couldn't reply in #%s: %s", message.channel, e)
            return
        self.db.update_share(message.id, reply_id=reply.id)
        self.db.log(message.guild.id, message.id, message.author.id, f"{kind}_post")
        log.info("%s post from %s in #%s", kind.title(), name, message.channel)

    @staticmethod
    async def read_card_file(message: discord.Message) -> Optional[dict]:
        """Character info from the first PNG/JSON attachment that is a character card, else None."""
        for a in message.attachments:
            if not character_links.looks_like_card_file(a.filename, a.size):
                continue
            try:
                info = character_links.parse_card_file(await a.read(), a.filename)
            except Exception as e:           # never let a bad file stop the prompt
                log.warning("Couldn't read attachment %s: %r", getattr(a, "filename", "?"), e)
                continue
            if info:
                return info
        return None

    def is_character_share(self, share: Share) -> bool:
        if share.songs:
            return character_links.is_character(share.song)
        return any(character_links.site_for(u) for u in share.links)

    async def handle_music_post(self, message: discord.Message) -> None:
        links = music_links.find_music_links(message.content)[:MAX_SONGS]
        if not links or self.db.get_share(message.id):
            return

        name = message.author.display_name
        self.db.create_share(message.id, message.guild.id, message.channel.id,
                             message.author.id, name, links)
        share = self.db.get_share(message.id)
        self.start_fetch(share)  # runs in the background while we reply

        view = self.public_reply_view(share)
        try:
            reply = await message.reply(
                T.PUBLIC_REPLY.format(name=escape_markdown(name)), view=view,
                mention_author=False, allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as e:
            log.warning("Couldn't reply in #%s: %s", message.channel, e)
            return
        self.db.update_share(message.id, reply_id=reply.id)
        self.db.log(message.guild.id, message.id, message.author.id, "shared")
        log.info("Suno share from %s in #%s (%d link%s)", name, message.channel,
                 len(links), "" if len(links) == 1 else "s")

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """If a member deletes their post, remove our reply too."""
        share = self.db.get_share(payload.message_id)
        if share and share.status not in ("dismissed", "expired", "deleted"):
            await self.delete_public_reply(share)
            self.db.update_share(share.post_id, status="deleted")

    # ------------------------------------------------------------ Suno data
    def start_fetch(self, share: Share) -> asyncio.Task:
        task = self.fetch_tasks.get(share.post_id)
        if task and not task.done():
            return task
        task = asyncio.create_task(self._fetch(share))
        self.fetch_tasks[share.post_id] = task
        task.add_done_callback(lambda _t, pid=share.post_id: self.fetch_tasks.pop(pid, None))
        return task

    async def _fetch(self, share: Share) -> list[dict]:
        async def one(url: str) -> dict:
            hit = self.cache.get(url)
            if hit and time.time() - hit[0] < CACHE_TTL:
                return hit[1]
            if character_links.site_for(url):
                info = await character_links.fetch_character(url, self.http)
                if info.get("title"):
                    self.cache[url] = (time.time(), info)
                return info
            if not music_links.is_suno(url):
                # YouTube, Spotify, etc.: title / artist / thumbnail only
                song = await music_links.fetch_link(url, self.http)
                if song.get("title"):
                    self.cache[url] = (time.time(), song)
                return song
            try:
                song = asdict(await suno_fetch.fetch_song(url, self.http))
            except Exception as e:
                log.warning("Suno fetch failed for %s: %r", url, e)
                return {"id": None, "url": url, "title": None, "error": str(e)}
            self.cache[url] = (time.time(), song)
            return song

        songs = list(await asyncio.gather(*(one(u) for u in share.links)))
        self.db.update_share(share.post_id, songs=songs)
        for s in songs:
            self.db.remember_song(s, share.guild_id, share.poster_id, share.post_id)
        return songs

    async def get_songs(self, share: Share, wait: float) -> Optional[list[dict]]:
        if share.songs is not None:
            return share.songs
        task = self.start_fetch(share)
        try:
            return await asyncio.wait_for(asyncio.shield(task), wait)
        except asyncio.TimeoutError:
            return None

    # ------------------------------------------------------------ themes
    def available_themes(self, share: Share) -> list[Season]:
        """Active themes this song hasn't been shared to yet."""
        done = {r["season_id"] for r in self.db.theme_posts_for(share.post_id, share.selected)}
        return [t for t in self.db.active_seasons(share.guild_id) if t.id not in done]

    async def share_to_theme(self, interaction: discord.Interaction, post_id: int,
                             season_id: int, note: Optional[str]) -> None:
        db = self.db
        share = db.get_share(post_id)
        season = db.get_season_by_id(season_id)
        if not season or not season.target_id:
            return await self.update_panel(interaction, share, notice=T.THEME_FAILED)
        if share.songs is None:
            await self.get_songs(share, wait=8)
            share = db.get_share(post_id)

        target = interaction.guild.get_channel_or_thread(season.target_id)
        if target is None:
            try:
                target = await self.bot.fetch_channel(season.target_id)
            except discord.HTTPException:
                target = None

        user = interaction.user
        song = share.song or {}
        title = song.get("title") or T.THREAD_FALLBACK_TITLE.format(name=share.poster_name)
        song_url = song.get("url") or share.link

        embed = discord.Embed(title=short(f"🎵 {title}", 256), url=song_url, color=PANEL_COLOR)
        embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
        if note:
            embed.description = short("\n".join(f"> {ln}" for ln in note.splitlines()), 4000)
        if share.line:
            embed.add_field(name="About the song", value=short(share.line, 1024), inline=False)
        if song.get("style"):
            embed.add_field(name=T.COLLAB_STYLE_FIELD, value=short(song["style"], 300), inline=False)
        if song.get("image_url"):
            embed.set_image(url=song["image_url"])

        view = ui.View(timeout=None)
        view.add_item(ui.Button(label=listen_label(song, song_url), url=song_url))
        view.add_item(ui.Button(label=T.COLLAB_ORIGINAL, url=share.jump_url))
        content = T.THEME_HEADER.format(label=season.label, mention=user.mention)

        try:
            if isinstance(target, discord.ForumChannel):
                made = await target.create_thread(name=short(title, 100), content=content, embed=embed, view=view)
                msg, where_id = made.message, made.thread.id
            elif isinstance(target, (discord.TextChannel, discord.Thread)):
                msg, where_id = await target.send(content, embed=embed, view=view), target.id
            else:
                raise ValueError(f"theme target {season.target_id} not found or unsupported")
        except (discord.HTTPException, ValueError) as e:
            log.warning("Couldn't share to theme %s: %s", season.name, e)
            return await self.update_panel(interaction, share, notice=T.THEME_FAILED)

        db.add_theme_post(post_id, season.id, share.selected, share.guild_id, where_id, msg.id)
        db.update_share(post_id, status="kept")
        share = db.get_share(post_id)
        await self.update_panel(interaction, share, notice=T.THEME_POSTED.format(label=season.label, url=msg.jump_url))
        await self.refresh_public_reply(share)
        db.log(share.guild_id, post_id, user.id, "theme")

    def reminder_text(self, share: Share, kind: str, name: str) -> str:
        """Friendly 'this channel is for...' note, pointing to a thread or the lounge."""
        cfg = self.db.get_config(share.guild_id)
        lounge = f"<#{cfg.lounge_channel_id}>" if cfg and cfg.lounge_channel_id else T.LOUNGE_FALLBACK
        channel = f"<#{share.channel_id}>"
        name = escape_markdown(name)
        if kind == "c":
            return T.COLLAB_REMINDER.format(name=name, channel=channel, lounge=lounge)
        music = (T.GALLERY_REMINDER_MUSIC.format(music=f"<#{cfg.music_channel_id}>")
                 if cfg and cfg.music_channel_id else "")
        return T.GALLERY_REMINDER.format(name=name, channel=channel, lounge=lounge, music=music)

    # ------------------------------------------------------------ source posts
    async def fetch_source(self, share: Share) -> Optional[discord.Message]:
        """The member's original message (fresh, so attachment links are current)."""
        channel = self.bot.get_channel(share.channel_id)
        if channel is None:
            return None
        try:
            return await channel.fetch_message(share.post_id)
        except discord.HTTPException:
            return None

    @staticmethod
    async def first_image(message: Optional[discord.Message]) -> tuple[Optional[discord.File], bool, bool]:
        """(image file to re-upload, was it spoilered, does the post have a video)."""
        if message is None:
            return None, False, False
        has_video = any((a.content_type or "").startswith("video/") for a in message.attachments)
        try:
            if not message.channel.permissions_for(message.guild.me).attach_files:
                return None, False, has_video     # can't re-upload: card goes without the image
        except Exception:
            pass
        for a in message.attachments:
            if (a.content_type or "").startswith("image/") and a.size <= MAX_IMAGE_BYTES:
                try:
                    return await a.to_file(spoiler=a.is_spoiler()), a.is_spoiler(), has_video
                except discord.HTTPException:
                    break
        return None, False, has_video

    async def put_card(self, share: Share, content: str, embed: discord.Embed, view: ui.View,
                       files: list[discord.File]) -> Optional[discord.Message]:
        """Turn the bot's public reply into a card (or post a fresh reply if it's gone)."""
        channel = self.bot.get_channel(share.channel_id)
        if channel is None:
            return None
        if share.reply_id:
            try:
                return await channel.get_partial_message(share.reply_id).edit(
                    content=content, embed=embed, view=view, attachments=files)
            except discord.NotFound:
                pass
        return await channel.get_partial_message(share.post_id).reply(
            content, embed=embed, view=view, files=files, mention_author=False,
            allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------ collab requests
    async def post_collab_request(self, interaction: discord.Interaction, post_id: int,
                                  type_values: list[str], note: Optional[str]) -> None:
        """Post a collab request card + its own thread.

        From the music panel: a new card in the collab channel.
        From a post in the collab channel: the bot's reply under the post becomes the card."""
        db, user = self.db, interaction.user
        share = db.get_share(post_id)
        from_music = share.kind == "music"

        if share.links and share.songs is None:
            await self.get_songs(share, wait=8)
            share = db.get_share(post_id)

        is_char = self.is_character_share(share)
        type_list = T.CHARACTER_COLLAB_TYPES if is_char else T.COLLAB_TYPES
        types = [t for t in type_list if t[0] in type_values]
        song = share.song or {}
        song_url = song.get("url") or share.link
        if is_char:
            title, icon = song.get("title") or T.CHARACTER_FALLBACK_TITLE.format(name=share.poster_name), "🎭"
        elif song.get("title"):
            title, icon = song["title"], "🎵"
        elif from_music:
            title, icon = T.THREAD_FALLBACK_TITLE.format(name=share.poster_name), "🎵"
        else:
            title, icon = T.COLLAB_IDEA_TITLE.format(name=share.poster_name), "💡"

        # Posts made in the collab channel: quote what they wrote so the card explains itself.
        src = None if from_music else await self.fetch_source(share)

        embed = discord.Embed(title=short(f"{icon} {title}", 256), url=song_url, color=PANEL_COLOR)
        embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
        idea = (src.content.strip() if src is not None else "")
        if idea and character_links.URL_RE.sub("", idea).strip():      # skip if the post is only link(s)
            embed.add_field(name=T.COLLAB_IDEA_FIELD, value=short(idea, 1024), inline=False)
        if is_char:
            if song.get("nsfw"):
                embed.add_field(name=T.CHARACTER_ABOUT_FIELD, value=T.CHARACTER_ADULT_NOTE, inline=False)
            elif song.get("description"):
                embed.add_field(name=T.CHARACTER_ABOUT_FIELD, value=short(song["description"], 300), inline=False)
            if song.get("tags") and not song.get("nsfw"):
                embed.add_field(name=T.CHARACTER_TAGS_FIELD,
                                value=short(", ".join(song["tags"][:10]), 200), inline=False)
            if song.get("creator"):
                embed.add_field(name=T.CHARACTER_CREATOR_FIELD, value=short(escape_markdown(song["creator"]), 100))
        embed.add_field(name=T.COLLAB_LOOKING_FOR,
                        value="\n".join(f"{e} {label}" for _, label, e, _ in types), inline=False)
        if note:
            embed.add_field(name=T.COLLAB_NOTE_FIELD, value=short(note, 1024), inline=False)
        if song.get("style"):
            embed.add_field(name=T.COLLAB_STYLE_FIELD, value=short(song["style"], 300), inline=False)

        files: list[discord.File] = []
        adult = bool(song.get("nsfw"))          # 18+-tagged character cards: no art on the request card
        if song.get("image_url") and not adult:
            if from_music:
                embed.set_image(url=song["image_url"])
            else:
                embed.set_thumbnail(url=song["image_url"])
        elif not from_music and not adult:
            f, spoiler, _ = await self.first_image(src)
            if f and not spoiler:
                files = [f]
                embed.set_thumbnail(url=f"attachment://{f.filename}")

        view = ui.View(timeout=None)
        view.add_item(InterestedButton(share.post_id, share.selected))
        if song_url:
            view.add_item(ui.Button(label=listen_label(song, song_url), url=song_url))
        view.add_item(ui.Button(label=T.COLLAB_ORIGINAL, url=share.jump_url))   # always link the original
        content = T.COLLAB_CARD_HEADER.format(mention=user.mention)

        try:
            if from_music:
                cfg = db.get_config(share.guild_id)
                channel = interaction.guild.get_channel(cfg.collab_channel_id) if cfg else None
                if channel is None:
                    return await self.update_panel(interaction, share, notice=T.COLLAB_NO_CHANNEL)
                card = await channel.send(content, embed=embed, view=view)
            else:
                card = await self.put_card(share, content, embed, view, files)
        except discord.HTTPException as e:
            log.warning("Couldn't post collab card: %s", e)
            card = None
        if card is None:
            if from_music:
                return await self.update_panel(interaction, share, notice=T.COLLAB_FAILED)
            return await tell(interaction, T.COLLAB_FAILED)

        thread = await self.thread_for_message(card, T.COLLAB_THREAD_NAME.format(title=title))
        if thread:
            try:
                await thread.send(T.COLLAB_THREAD_INTRO.format(title=escape_markdown(title), mention=user.mention)
                                  + "\n" + T.COLLAB_THREAD_ORIGINAL.format(url=share.jump_url),
                                  allowed_mentions=discord.AllowedMentions(users=[user]))
            except discord.HTTPException as e:
                log.warning("Couldn't post in collab thread: %s", e)

        db.add_collab(card.id, share.guild_id, card.channel.id, share.post_id, share.selected,
                      share.poster_id, song.get("id"), [t[0] for t in types], note, None)
        db.update_share(share.post_id, status="kept")
        share = db.get_share(post_id)
        if from_music:
            await self.update_panel(interaction, share, notice=T.COLLAB_POSTED.format(url=card.jump_url))
            await self.refresh_public_reply(share)
        else:
            await tell(interaction, T.COLLAB_POSTED_HERE.format(url=(thread or card).jump_url))
        db.log(share.guild_id, share.post_id, user.id, "collab")

    # ------------------------------------------------------------ gallery
    async def post_gallery_collab(self, interaction: discord.Interaction, post_id: int, *,
                                  title: Optional[str], members: list, others: Optional[str],
                                  note: Optional[str]) -> None:
        """Present a collab result: congratulations card under the post + a comment thread."""
        db, user = self.db, interaction.user
        share = db.get_share(post_id)
        if share.links and share.songs is None:
            await self.get_songs(share, wait=8)
            share = db.get_share(post_id)
        song = share.song or {}
        title = title or song.get("title") or T.GALLERY_DEFAULT_TITLE

        # Picker values are usually Member objects, but can arrive as plain ids.
        collaborators = []
        for m in members:
            if isinstance(m, (str, int)):
                m = interaction.guild.get_member(int(m)) or discord.Object(id=int(m))
            if m.id != user.id and not getattr(m, "bot", False) and m.id not in {c.id for c in collaborators}:
                collaborators.append(m)
        credits = [user.mention] + [f"<@{m.id}>" for m in collaborators]
        if others:
            credits.append(escape_markdown(others))

        embed = discord.Embed(title=short(f"🎉 {title}", 256), url=song.get("url"), color=GALLERY_COLOR)
        desc = [T.GALLERY_CONGRATS]
        embed.add_field(name=T.GALLERY_CREATED_BY, value=short(" · ".join(credits), 1024), inline=False)
        if note:
            embed.add_field(name=T.GALLERY_ABOUT, value=short(note, 1024), inline=False)
        if song.get("style"):
            embed.add_field(name=T.COLLAB_STYLE_FIELD, value=short(song["style"], 200), inline=False)

        files: list[discord.File] = []
        f, spoiler, has_video = await self.first_image(await self.fetch_source(share))
        if f and not spoiler:
            files = [f]
            embed.set_image(url=f"attachment://{f.filename}")
        elif f and spoiler:
            files = [f]                       # stays spoilered as a plain attachment
            desc.append(T.GALLERY_SPOILER_NOTE)
        elif song.get("image_url"):
            embed.set_image(url=song["image_url"])
        if has_video:
            desc.append(T.GALLERY_VIDEO_NOTE)
        embed.description = "\n".join(desc)
        embed.set_footer(text=T.GALLERY_FOOTER)

        view = ui.View(timeout=None)
        if song.get("url"):
            view.add_item(ui.Button(label=listen_label(song, song["url"]), url=song["url"]))

        try:
            card = await self.put_card(share, T.GALLERY_HEADER, embed, view, files)
        except discord.HTTPException as e:
            log.warning("Couldn't post gallery card: %s", e)
            card = None
        if card is None:
            return await tell(interaction, T.THREAD_FAILED)

        thread = await self.thread_for_message(card, T.GALLERY_THREAD_NAME.format(title=title))
        if thread:
            people = " ".join([user.mention] + [f"<@{m.id}>" for m in collaborators])
            try:
                await thread.send(
                    T.GALLERY_THREAD_INTRO.format(people=people, title=escape_markdown(title)),
                    allowed_mentions=discord.AllowedMentions(users=[user, *collaborators]))
            except discord.HTTPException as e:
                log.warning("Couldn't post in gallery thread: %s", e)

        db.add_gallery_entry(card.id, share.post_id, share.guild_id, card.channel.id,
                             thread.id if thread else None, user.id, title,
                             [m.id for m in collaborators], others, note)
        db.update_share(share.post_id, status="kept")
        await tell(interaction, T.GALLERY_POSTED.format(url=(thread or card).jump_url))
        db.log(share.guild_id, share.post_id, user.id, "gallery_collab")

    # ------------------------------------------------------------ public reply
    def public_reply_view(self, share: Share) -> ui.View:
        """Enhance Sharing (poster only) + a Like link per song so anyone can like it at the source."""
        view = ui.View(timeout=None)
        view.add_item(OpenPanelButton(share.post_id))
        per_site: dict[str, int] = {}
        for url in share.links[:4]:
            site = "Suno" if "suno." in url.lower() else (music_links.site_for(url) or "the web")
            per_site[site] = per_site.get(site, 0) + 1
            label = (T.LIKE_BUTTON.format(site=site) if per_site[site] == 1
                     else T.LIKE_BUTTON_N.format(n=per_site[site], site=site))
            if len(url) <= 512:
                view.add_item(ui.Button(label=short(label, 80), url=url))
        return view

    async def compact_public_reply(self, share: Share) -> None:
        """Unused (or 'just sharing') music reply: shorter text, same buttons, so Like stays."""
        channel = self.bot.get_channel(share.channel_id)
        if channel is None or not share.reply_id:
            return
        try:
            await channel.get_partial_message(share.reply_id).edit(
                content=T.PUBLIC_REPLY_COMPACT.format(name=escape_markdown(share.poster_name or "friend")),
                view=self.public_reply_view(share))
        except discord.HTTPException:
            pass

    def public_reply_content(self, share: Share) -> str:
        name = escape_markdown(share.poster_name or "friend")
        if share.status in ("expired", "dismissed") and not (
                share.line or share.thread_id or self.db.collabs_for_post(share.post_id)
                or self.db.theme_posts_for(share.post_id)):
            return T.PUBLIC_REPLY_COMPACT.format(name=name)
        lines = [T.PUBLIC_REPLY.format(name=name)]
        if share.line:
            lines.append(T.REPLY_LINE.format(name=name, line=share.line))
        for r in self.db.theme_posts_for(share.post_id):
            label = f"{r['emoji'] or '🏷️'} {r['name']}"
            lines.append(T.REPLY_THEME.format(label=label, url=channel_url(r["guild_id"], r["channel_id"], r["message_id"])))
        for c in self.db.collabs_for_post(share.post_id):
            lines.append(T.REPLY_COLLAB.format(url=channel_url(c["guild_id"], c["channel_id"], c["card_id"])))
        if share.thread_id:
            lines.append(T.REPLY_THREAD.format(url=channel_url(share.guild_id, share.thread_id)))
        return "\n".join(lines)

    async def refresh_public_reply(self, share: Share) -> None:
        channel = self.bot.get_channel(share.channel_id)
        if channel is None:
            return
        view = self.public_reply_view(share)
        content = self.public_reply_content(share)
        if share.reply_id:
            try:
                await channel.get_partial_message(share.reply_id).edit(content=content, view=view)
                return
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                log.warning("Couldn't edit reply: %s", e)
                return
        # Reply is gone (expired or removed): post a fresh one.
        try:
            reply = await channel.get_partial_message(share.post_id).reply(
                content, view=view, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            self.db.update_share(share.post_id, reply_id=reply.id)
        except discord.HTTPException as e:
            log.warning("Couldn't re-post reply: %s", e)

    async def delete_public_reply(self, share: Share) -> None:
        channel = self.bot.get_channel(share.channel_id)
        if channel is None or not share.reply_id:
            return
        try:
            await channel.get_partial_message(share.reply_id).delete()
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------ private panel
    def render_panel(self, share: Share, notice: Optional[str] = None) -> tuple[discord.Embed, ui.View]:
        song = share.song or {}
        title = song.get("title") or "your song"
        thanks = T.THANK_YOU[share.post_id % len(T.THANK_YOU)].format(title=escape_markdown(title))

        desc = []
        if notice:
            desc.append(f"✅ {notice}")
        desc.append(thanks)
        if share.songs is None:
            desc.append(T.PANEL_LOADING)
        embed = discord.Embed(description="\n\n".join(desc), color=PANEL_COLOR)
        if song.get("title"):
            embed.title = short(f"🎵 {song['title']}", 256)
            embed.url = song.get("url")
        if song.get("style"):
            embed.add_field(name="Style", value=short(song["style"], 300), inline=False)
        if song.get("source") == "link":
            parts = [p for p in (song.get("site"), song.get("artist")) if p]
            if parts:
                embed.add_field(name=T.PANEL_FROM, value=short(" · ".join(parts), 200), inline=False)
        if share.line:
            embed.add_field(name="Your line", value=short(share.line, 300), inline=False)
        if song.get("image_url"):
            embed.set_thumbnail(url=song["image_url"])

        # The pitch, right above the buttons.
        active_themes = self.db.active_seasons(share.guild_id)
        pitch = []
        for key, text in T.PANEL_PITCH:
            if key == "theme":
                if not active_themes:
                    continue
                text = text.format(themes=", ".join(f"**{t.label}**" for t in active_themes[:3]))
            pitch.append(text)
        embed.add_field(name=T.PANEL_PITCH_TITLE, value=short("\n".join(pitch), 1024), inline=False)
        embed.set_footer(text=T.PANEL_FOOTER)

        view = ui.View(timeout=None)
        if len(share.links) > 1:
            options = []
            for i, link in enumerate(share.links):
                s = share.songs[i] if share.songs and i < len(share.songs) else {}
                options.append(discord.SelectOption(
                    label=short(s.get("title") or f"Song {i + 1}", 100), value=str(i),
                    description=short(link, 100), default=(i == share.selected)))
            view.add_item(SongPick(share.post_id, options))

        view.add_item(PanelButton("line", share.post_id, T.BTN_LINE_EDIT if share.line else T.BTN_LINE))

        if share.thread_id:
            view.add_item(ui.Button(label=T.BTN_THREAD_DONE, url=channel_url(share.guild_id, share.thread_id)))
        else:
            view.add_item(PanelButton("thread", share.post_id, T.BTN_THREAD))

        collab = self.db.get_collab_for(share.post_id, share.selected)
        if collab:
            view.add_item(ui.Button(label=T.BTN_COLLAB_DONE, url=channel_url(
                collab["guild_id"], collab["channel_id"], collab["card_id"])))
        else:
            view.add_item(PanelButton("collab", share.post_id, T.BTN_COLLAB))

        # Theme button: only while at least one theme is active.
        active = self.db.active_seasons(share.guild_id)
        if active:
            posted = {r["season_id"]: r for r in self.db.theme_posts_for(share.post_id, share.selected)}
            if len(active) == 1:
                t = active[0]
                if t.id in posted:
                    r = posted[t.id]
                    view.add_item(ui.Button(
                        label=short(T.BTN_THEME_DONE.format(emoji=t.emoji or "🏷️", name=t.name), 80),
                        url=channel_url(share.guild_id, r["channel_id"], r["message_id"])))
                else:
                    view.add_item(PanelButton(
                        "theme", share.post_id, T.BTN_THEME_ONE.format(emoji=t.emoji or "🏷️", name=t.name)))
            elif any(t.id not in posted for t in active):
                view.add_item(PanelButton("theme", share.post_id, T.BTN_THEME_MANY))

        view.add_item(PanelButton("done", share.post_id, T.BTN_DONE))
        return embed, view

    async def update_panel(self, interaction: discord.Interaction, share: Share,
                           notice: Optional[str] = None) -> None:
        """Re-draw the private panel after a pop-up was submitted (interaction already deferred)."""
        embed, view = self.render_panel(share, notice)
        try:
            await interaction.edit_original_response(embed=embed, view=view)
        except discord.HTTPException:
            if notice:
                await tell(interaction, notice)

    async def just_sharing(self, interaction: discord.Interaction, share: Share) -> None:
        await interaction.response.edit_message(content=T.JUST_SHARING_REPLY, embed=None, view=None)
        if share.status == "pending":
            self.db.update_share(share.post_id, status="dismissed")
            await self.compact_public_reply(share)      # keeps the Like buttons for listeners
        self.db.log(share.guild_id, share.post_id, interaction.user.id, "just_sharing")

    # ------------------------------------------------------------ threads
    async def thread_for_message(self, message: discord.Message | discord.PartialMessage,
                                 name: str) -> Optional[discord.Thread]:
        """Create a thread on a message, or return the one that already exists."""
        guild = message.guild
        existing = guild.get_thread(message.id) if guild else None
        if existing:
            return existing
        try:
            return await message.create_thread(name=short(name, 100), auto_archive_duration=10080)
        except discord.HTTPException as e:
            if getattr(e, "code", None) == 160004:  # thread already exists for this message
                try:
                    ch = await self.bot.fetch_channel(message.id)
                    return ch if isinstance(ch, discord.Thread) else None
                except discord.HTTPException:
                    return None
            log.warning("Couldn't create thread: %s", e)
            return None

    # ------------------------------------------------------------ housekeeping
    @tasks.loop(seconds=60)
    async def housekeeping(self) -> None:
        """Every minute: shrink/remove unused bot prompts past their timeout, trim the cache."""
        try:
            for share in self.db.expired_shares():
                if share.kind == "music":
                    await self.compact_public_reply(share)   # music replies stay (Like buttons)
                else:
                    await self.delete_public_reply(share)    # collab/gallery prompts go away
                self.db.update_share(share.post_id, status="expired")
            now = time.time()
            for url in [u for u, (t, _) in self.cache.items() if now - t > CACHE_TTL]:
                self.cache.pop(url, None)
        except Exception:
            log.exception("Housekeeping failed (will retry next minute)")

    @housekeeping.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SunoFlow(bot))
