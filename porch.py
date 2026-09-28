# AIAVBOT - a Discord bot for the Dreamers AI Hub community
# Copyright (C) 2026 Oak
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU Affero General Public License v3.0 or later. See LICENSE.
"""
porch.py - The character porch: live character channels backed by The Stoop partner API.

Two channels, set with /aiav setup:
  porch     SFW. Members' character shares + the SFW arrival feed (rating=sfw).
  porch_18  Must be a Discord age-restricted channel. Members' shares + the 18+ feed (rating=nsfw).

What happens:
  - A member shares a character (Stoop / Chub / JanitorAI / ... link, or a PNG/JSON card file)
    -> the bot replies with a card: art, summary, creator, tags + 👋 Say hi · ✅ I met · 🤝 Start a collab · 🚩 Report.
  - Arrival feed (off by default): new Stoop characters get an arrival card.
  - Stoop cards stay in sync: edits are applied in place, removed cards are hidden, and they're restored if
    they come back (see AIAVBOT_Stoop_Integration_Plan.md, section 5).

Rating safety (our keys can SEE 18+ cards through detail/images, so we always check `nsfw` ourselves):
  - An 18+ card is shown in full ONLY in the age-restricted porch.
  - Everywhere else it is name + link + "🔞 18+ character", and its art is never downloaded for it.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord import ui
from discord.ext import commands, tasks
from discord.utils import escape_markdown

import character_links
import stoop_api
import suno_fetch
import texts as T
from storage import GuildConfig, Storage
from stoop_api import StoopClient, StoopError, StoopRateLimited, StoopUnauthorized

log = logging.getLogger("aiavbot.porch")

PORCH_COLOR = 0xE7A15A
ADULT_COLOR = 0x8B3A62
GREY = 0x6B6B6B

FEED_MINUTES = 5                  # how often the since-polls run
RECHECK_MINUTES = 10              # how often removal re-checks run
RECHECK_PER_CYCLE = 20            # detail calls per re-check cycle (about 2/min)
RECENT_DAYS = 30
RECENT_EVERY = 6 * 3600           # re-check cards shown in the last 30 days every 6 h
OLD_EVERY = 7 * 86400             # older ones weekly
FRESH_FOR = 30 * 60               # card data younger than this isn't re-fetched on use
GONE_AFTER = 7 * 86400            # 404 for this long -> tombstone
STATS_EDIT_EVERY = 6 * 3600       # stats-only edits (downloads, mod pick) at most this often
MAX_ARRIVALS_PER_POLL = 5         # per channel; the rest wait for the next poll
STALE_SINCE = 2 * 86400           # a watermark older than this restarts from now (no huge catch-up)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def short(text: Optional[str], limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def get_porch(interaction: discord.Interaction) -> "Porch":
    return interaction.client.get_cog("Porch")  # type: ignore[return-value]


async def tell(interaction: discord.Interaction, text: str, **kwargs) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True, **kwargs)
        else:
            await interaction.response.send_message(text, ephemeral=True, **kwargs)
    except discord.HTTPException:
        pass


async def ack(interaction: discord.Interaction) -> None:
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except discord.NotFound:
        log.warning("Porch: reply window expired; continuing anyway")
    except discord.InteractionResponded:
        pass


def card_key(post: dict) -> str:
    """What 'I met' counts are stored against: the Stoop id, or the porch card's message for other sites."""
    return post["stoop_id"] or f"m{post['message_id']}"


def jump(guild_id: int, channel_id: int, message_id: Optional[int] = None) -> str:
    base = f"https://discord.com/channels/{guild_id}/{channel_id}"
    return f"{base}/{message_id}" if message_id else base


def neutral_placeholder(preview: dict, stoop_id: str) -> dict:
    """Name + link only, for a Stoop card we couldn't check with the API (its rating is unknown)."""
    info = dict(preview)
    info.update(id=stoop_id, url=stoop_api.card_url(stoop_id), site="The Stoop", description=None, tags=[],
                image_url=None, stoop=True, stoop_asset=None, unverified=True)
    return info


def stoop_key_from_env() -> Optional[str]:
    if os.getenv("STOOP_API_KEY_18"):
        log.info("STOOP_API_KEY_18 is ignored: one key per instance (STOOP_API_KEY) covers SFW and 18+.")
    return os.getenv("STOOP_API_KEY") or os.getenv("STOOP_API_KEY_SFW")


# --------------------------------------------------------------------------- #
# Rendering (pure: no network, easy to test)
# --------------------------------------------------------------------------- #

def desired_state(char: dict, adult_ok: bool) -> str:
    return "adult" if char.get("nsfw") and not adult_ok else "full"


def render_card(char: dict, *, state: str, kind: str, message_id: int, met: int,
                stoop: Optional[dict] = None, poster_name: Optional[str] = None,
                image_name: Optional[str] = None, porch18_id: Optional[int] = None,
                big_image: bool = True) -> tuple[Optional[str], discord.Embed, Optional[ui.View]]:
    """(content, embed, view) for a porch card.

    char   character dict (stoop_api.to_character_info or character_links); name in "title"
    state  full | adult | missing | gone
    stoop  the raw Stoop card, for downloads / mod pick / version / creator verification
    image_name  filename of the attached art (full state only); None = use char["image_url"] if any
    """
    name = char.get("title") or "this character"
    site = char.get("site") or "the web"
    url = char.get("url")

    if state in ("missing", "gone"):
        embed = discord.Embed(description=T.PORCH_MISSING if state == "missing" else T.PORCH_GONE, color=GREY)
        return "", embed, None

    if state == "adult":
        where = T.PORCH_ADULT_WHERE.format(channel=f"<#{porch18_id}>") if porch18_id else ""
        embed = discord.Embed(title=short(T.PORCH_ADULT_TITLE.format(name=name), 256), url=url,
                              description=T.PORCH_ADULT_TEXT.format(site=site, porch18=where), color=ADULT_COLOR)
        view = ui.View(timeout=None)
        view.add_item(PorchButton("report", message_id))
        if url and len(url) <= 512:
            view.add_item(ui.Button(label=short(T.PORCH_BTN_OPEN.format(site=site), 80), url=url))
        content = (T.PORCH_SHARED.format(poster=escape_markdown(poster_name or "Someone"), name=escape_markdown(name))
                   if kind == "share" else None)
        return content, embed, view

    # full
    embed = discord.Embed(title=short(f"🎭 {name}", 256), url=url, color=ADULT_COLOR if char.get("nsfw") else PORCH_COLOR)
    if char.get("description"):
        embed.description = short(char["description"], 500)
    creator = char.get("creator")
    if creator:
        verified = bool(stoop and isinstance(stoop.get("creator"), dict) and stoop["creator"].get("verification"))
        value = escape_markdown(creator) + (T.PORCH_VERIFIED if verified else "")
        oc = stoop_api.original_creator_name(stoop) if stoop else None
        if oc and oc != creator:
            value += "\n" + T.PORCH_BASED_ON.format(name=escape_markdown(oc))
        embed.add_field(name=T.PORCH_CREATOR_FIELD, value=short(value, 1024), inline=True)
    if char.get("tags"):
        embed.add_field(name=T.PORCH_TAGS_FIELD, value=short(", ".join(char["tags"][:10]), 300), inline=True)
    if stoop:
        bits = []
        if stoop.get("type") in T.PORCH_TYPES:
            bits.append(T.PORCH_TYPES[stoop["type"]])
        if isinstance(stoop.get("downloadCount"), int):
            bits.append(T.PORCH_DOWNLOADS.format(n=stoop["downloadCount"]))
        if stoop.get("modPick"):
            bits.append(T.PORCH_MOD_PICK)
        if stoop.get("version"):
            bits.append(T.PORCH_VERSION.format(n=stoop["version"]))
        if bits:
            embed.add_field(name=T.PORCH_STATS_FIELD, value=" · ".join(bits), inline=False)
    if image_name:
        img = f"attachment://{image_name}"
        embed.set_image(url=img) if big_image else embed.set_thumbnail(url=img)
    elif char.get("image_url"):
        embed.set_image(url=char["image_url"]) if big_image else embed.set_thumbnail(url=char["image_url"])
    if met <= 0:
        footer = T.PORCH_MET_NONE.format(name=name)
    elif met == 1:
        footer = T.PORCH_MET_ONE.format(name=name)
    else:
        footer = T.PORCH_MET_MANY.format(n=met, name=name)
    embed.set_footer(text=short(footer, 200))

    view = ui.View(timeout=None)
    view.add_item(PorchButton("hi", message_id))
    view.add_item(PorchButton("met", message_id, name))
    view.add_item(PorchButton("collab", message_id))
    view.add_item(PorchButton("report", message_id))
    if url and len(url) <= 512:
        view.add_item(ui.Button(label=short(T.PORCH_BTN_OPEN.format(site=site), 80), url=url))

    if kind == "arrival":
        content = T.PORCH_ARRIVAL.format(name=escape_markdown(name))
    else:
        content = T.PORCH_SHARED.format(poster=escape_markdown(poster_name or "Someone"), name=escape_markdown(name))
    return content, embed, view


# --------------------------------------------------------------------------- #
# Buttons and forms
# --------------------------------------------------------------------------- #

class PorchButton(ui.DynamicItem[ui.Button], template=r"porch:(?P<action>hi|met|collab|report):(?P<mid>\d+)"):
    STYLES = {"hi": discord.ButtonStyle.primary, "met": discord.ButtonStyle.success,
              "collab": discord.ButtonStyle.secondary, "report": discord.ButtonStyle.secondary}

    def __init__(self, action: str, mid: int, name: str = ""):
        label = {"hi": T.PORCH_BTN_HI, "met": T.PORCH_BTN_MET.format(name=name or "them"),
                 "collab": T.PORCH_BTN_COLLAB, "report": T.PORCH_BTN_REPORT}[action]
        super().__init__(ui.Button(label=short(label, 80), style=self.STYLES[action],
                                   custom_id=f"porch:{action}:{mid}"))
        self.action, self.mid = action, mid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["mid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        porch = get_porch(interaction)
        try:
            # The card being clicked. Its message id is in the button (a card never moves).
            post = porch.db.porch_post(self.mid)
            if not post:
                return await tell(interaction, T.PORCH_CARD_GONE)
            if self.action == "report":
                return await interaction.response.send_modal(ReportModal(porch, post))
            if post["state"] in ("missing", "gone"):
                return await tell(interaction, T.PORCH_STALE)
            if post["state"] == "adult":                       # 18+ outside the 18+ porch: report/link only
                return await tell(interaction, T.PORCH_ADULT_BUTTONS)
            if self.action == "collab":
                return await porch.start_collab(interaction, post)     # opens a pop-up: must answer first
            await ack(interaction)
            if self.action == "hi":
                await porch.say_hi(interaction, post)
            elif self.action == "met":
                await porch.met(interaction, post)
        except Exception:
            log.exception("Porch button %s failed", self.action)
            await tell(interaction, T.GENERIC_ERROR)


class ReportModal(ui.Modal):
    def __init__(self, porch: "Porch", post: dict):
        super().__init__(title=T.PORCH_REPORT_TITLE, timeout=900)
        self.porch, self.mid = porch, post["message_id"]
        self.reason = ui.Select(options=[discord.SelectOption(label=label, value=value, emoji=emoji)
                                         for value, label, emoji in T.PORCH_REPORT_REASONS],
                                min_values=1, max_values=1, required=True)
        self.add_item(ui.Label(text=T.PORCH_REPORT_REASON, component=self.reason))
        self.details = ui.TextInput(style=discord.TextStyle.paragraph, placeholder=T.PORCH_REPORT_PLACEHOLDER,
                                    max_length=800, required=False)
        self.add_item(ui.Label(text=T.PORCH_REPORT_DETAILS, component=self.details))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await ack(interaction)
        reason = self.reason.values[0] if self.reason.values else "other"
        await self.porch.report(interaction, self.mid, reason, self.details.value.strip() or None)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Report form failed", exc_info=error)
        await tell(interaction, T.GENERIC_ERROR)


class HowWasItModal(ui.Modal):
    def __init__(self, porch: "Porch", mid: int):
        super().__init__(title=T.PORCH_HOW_TITLE, timeout=900)
        self.porch, self.mid = porch, mid
        self.text = ui.TextInput(style=discord.TextStyle.short, placeholder=T.PORCH_HOW_PLACEHOLDER,
                                 min_length=2, max_length=300, required=True)
        self.add_item(ui.Label(text=T.PORCH_HOW_LABEL, component=self.text))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await ack(interaction)
        await self.porch.post_how_it_went(interaction, self.mid, " ".join(self.text.value.split()))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("How-was-it form failed", exc_info=error)
        await tell(interaction, T.GENERIC_ERROR)


class HowWasItView(ui.View):
    """Private follow-up after 'I met': one button that opens the How-was-it form."""

    def __init__(self, porch: "Porch", mid: int):
        super().__init__(timeout=600)
        self.porch, self.mid = porch, mid

    @ui.button(label=T.PORCH_MET_TELL, style=discord.ButtonStyle.primary)
    async def tell_btn(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await interaction.response.send_modal(HowWasItModal(self.porch, self.mid))


# --------------------------------------------------------------------------- #
# The cog
# --------------------------------------------------------------------------- #

class Porch(commands.Cog):
    def __init__(self, bot: commands.Bot, client: Optional[StoopClient] = None):
        self.bot = bot
        self.db: Storage = bot.db  # type: ignore[attr-defined]
        self.client = client or StoopClient(stoop_key_from_env(), os.getenv("STOOP_API_BASE") or None)
        self.http: Optional[aiohttp.ClientSession] = None
        self.images_dir = Path(self.db.path).parent / "stoop_images"
        self._stats_cache: tuple[float, Optional[dict]] = (0.0, None)
        self._poll_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self.http = aiohttp.ClientSession(headers=suno_fetch.HEADERS, timeout=suno_fetch.TIMEOUT)
        self.bot.add_dynamic_items(PorchButton)
        self.feed_loop.change_interval(minutes=FEED_MINUTES)
        self.feed_loop.start()
        self.recheck_loop.start()
        log.info("Porch loaded (The Stoop API: %s)", "key set" if self.client.enabled else "no key")

    async def cog_unload(self) -> None:
        self.feed_loop.cancel()
        self.recheck_loop.cancel()
        await self.client.close()
        if self.http:
            await self.http.close()

    # ------------------------------------------------------------ config helpers
    def porch_channels(self, cfg: Optional[GuildConfig]) -> dict[int, bool]:
        """{channel_id: adult_ok} for this guild's porch channels."""
        out: dict[int, bool] = {}
        if cfg and cfg.porch_channel_id:
            out[cfg.porch_channel_id] = False
        if cfg and cfg.porch18_channel_id:
            ch = self.bot.get_channel(cfg.porch18_channel_id)
            # Only a channel Discord marks age-restricted may show 18+ cards in full.
            out[cfg.porch18_channel_id] = bool(ch is not None and getattr(ch, "is_nsfw", lambda: False)())
        return out

    # ------------------------------------------------------------ images
    async def image_file(self, asset_id: Optional[str]) -> Optional[discord.File]:
        """The card's art (WebP thumb, <= 512 px) as an attachment, cached on disk by asset id."""
        if not asset_id or not self.client.enabled:
            return None
        safe_id = "".join(c for c in asset_id if c.isalnum() or c == "-")[:64]
        path = self.images_dir / f"{safe_id}.webp"
        data = None
        if path.exists():
            try:
                data = path.read_bytes()
            except OSError:
                data = None
        if data is None:
            try:
                got = await self.client.image(asset_id, thumb=True)
            except StoopError as e:
                log.warning("Couldn't download Stoop image: %s", e)
                return None
            if not got:
                return None
            data = got[0]
            try:
                self.images_dir.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            except OSError as e:
                log.warning("Couldn't cache Stoop image: %s", e)
        return discord.File(io.BytesIO(data), filename=f"stoop_{safe_id[:8]}.webp")

    def forget_image(self, asset_id: Optional[str]) -> None:
        if not asset_id:
            return
        safe_id = "".join(c for c in asset_id if c.isalnum() or c == "-")[:64]
        try:
            (self.images_dir / f"{safe_id}.webp").unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------ Stoop data
    async def lookup(self, stoop_id: str, *, max_age: float = FRESH_FOR) -> tuple[str, Optional[dict]]:
        """("ok", card) · ("missing", None) when the API says 404 · ("error", cached card or None)
        when the API can't be asked (no key, down, rate-limited). Card data younger than max_age is reused."""
        rec = self.db.stoop_card(stoop_id)
        cached = rec["data"] if rec and rec["status"] == "live" else None
        if cached and (rec["fetched_at"] or 0) > time.time() - max_age:
            return "ok", cached
        if not self.client.enabled:
            return "error", cached
        try:
            card = await self.client.card(stoop_id)
        except StoopError as e:
            log.warning("Stoop detail failed: %s", e)
            return "error", cached
        if card:
            await self.apply_card(card)
            return "ok", card
        if rec:
            await self.on_missing(stoop_id)
        else:
            self.db.note_stoop_unavailable(stoop_id)
        return "missing", None

    async def fresh_card(self, stoop_id: str, *, max_age: float = FRESH_FOR) -> Optional[dict]:
        """Card data (re-fetched if older than max_age), or None if unavailable."""
        return (await self.lookup(stoop_id, max_age=max_age))[1]

    def char_for_post(self, post: dict) -> tuple[Optional[dict], Optional[dict]]:
        """(character dict, raw Stoop card) for a porch post."""
        if post["stoop_id"]:
            rec = self.db.stoop_card(post["stoop_id"])
            if rec and rec["data"]:
                return stoop_api.to_character_info(rec["data"]), rec["data"]
            return post["info"], None
        return post["info"], None

    # ------------------------------------------------------------ member shares
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        cfg = self.db.get_config(message.guild.id)
        if not cfg or message.channel.id not in (cfg.porch_channel_id, cfg.porch18_channel_id):
            return
        try:
            await self.handle_share(message, cfg)
        except Exception:
            log.exception("Porch share failed")

    async def handle_share(self, message: discord.Message, cfg: GuildConfig) -> None:
        if self.db.porch_post_for_member_post(message.id):
            return
        adult_ok = self.porch_channels(cfg).get(message.channel.id, False) and \
            message.channel.id == cfg.porch18_channel_id
        links = character_links.find_character_links(message.content)
        stoop_card = None
        stoop_id = None
        char: Optional[dict] = None

        if links:
            url = links[0]
            stoop_id = stoop_api.card_id_from_url(url)
            result, stoop_card = ("error", None)
            if stoop_id and self.client.enabled:
                result, stoop_card = await self.lookup(stoop_id)
                if result == "missing":
                    # Not available (in review, removed...). Don't show anything from the link preview.
                    await self._reply(message, T.PORCH_UNAVAILABLE_LINK.format(site="The Stoop"))
                    return
            if stoop_card is not None:
                existing = self.db.porch_post_in_channel(stoop_id, message.channel.id)
                if existing and existing["state"] in ("full", "adult"):
                    self._track_member_post(message, stoop_id, adult_ok)
                    await self._reply(message, T.PORCH_ALREADY_HERE.format(
                        name=escape_markdown(stoop_card.get("name") or "This character"),
                        url=jump(message.guild.id, message.channel.id, existing["message_id"])))
                    return
                char = stoop_api.to_character_info(stoop_card)
            else:
                # Other sites, or The Stoop without a key: the public link preview.
                char = await character_links.fetch_character(url, self.http)
                if stoop_id and self.client.enabled:
                    # The API couldn't be asked right now. Show only the name and link (no art or text, since
                    # we can't check the rating) and re-check it with the API as soon as possible.
                    char = neutral_placeholder(char, stoop_id)
                    self.db.note_stoop_pending(stoop_id)
                else:
                    stoop_id = None
                if not char.get("title"):
                    return                                   # couldn't read it; let the chat carry on
        else:
            for a in message.attachments:
                if character_links.looks_like_card_file(a.filename, a.size):
                    try:
                        char = character_links.parse_card_file(await a.read(), a.filename)
                    except Exception as e:
                        log.warning("Couldn't read card file: %r", e)
                        char = None
                    if char:
                        break
            if not char:
                return                                       # regular chat: nothing to do

        state = desired_state(char, adult_ok)
        placeholder_id = 0
        files = []
        image_name = None
        if state == "full" and char.get("stoop_asset"):
            f = await self.image_file(char["stoop_asset"])
            if f:
                files, image_name = [f], f.filename
        content, embed, view = render_card(
            char, state=state, kind="share", message_id=placeholder_id,
            met=self.db.met_count(stoop_id, message.guild.id) if stoop_id else 0,
            stoop=stoop_card, poster_name=message.author.display_name, image_name=image_name,
            porch18_id=cfg.porch18_channel_id, big_image=False)
        try:
            reply = await message.reply(content, embed=embed, files=files, mention_author=False,
                                        allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("Couldn't reply in porch: %s", e)
            return
        # Buttons need the card's own message id, so they're added right after posting.
        _, _, view = render_card(char, state=state, kind="share", message_id=reply.id, met=0, stoop=stoop_card,
                                 porch18_id=cfg.porch18_channel_id)
        try:
            await reply.edit(view=view)
        except discord.HTTPException as e:
            log.warning("Couldn't add porch buttons: %s", e)
        self.db.add_porch_post(reply.id, message.guild.id, message.channel.id, "share", adult_ok=adult_ok,
                               post_id=message.id, poster_id=message.author.id, stoop_id=stoop_id,
                               info=None if stoop_card else char, state=state,
                               shown_version=stoop_card.get("version") if stoop_card else None,
                               shown_asset=char.get("stoop_asset") if state == "full" else None)
        if stoop_id:
            self._track_member_post(message, stoop_id, adult_ok)
        self.db.log(message.guild.id, message.id, message.author.id, "porch_share")
        log.info("Porch share from %s in #%s (%s)", message.author.display_name, message.channel,
                 "Stoop" if stoop_id else char.get("site"))

    def _track_member_post(self, message: discord.Message, stoop_id: str, adult_ok: bool) -> None:
        rec = self.db.stoop_card(stoop_id)
        already_adult = bool(rec and rec["nsfw"])     # shared as 18+ from the start: nothing to warn about later
        self.db.add_stoop_ref(message.id, stoop_id, message.guild.id, message.channel.id, "member_post", adult_ok,
                              state="alerted" if already_adult else "ok")

    async def _reply(self, message: discord.Message, text: str) -> None:
        try:
            await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """Member deleted their share -> remove our card too. Our card deleted by a mod -> forget it."""
        post = self.db.porch_post_for_member_post(payload.message_id)
        if post:
            ch = self.bot.get_channel(post["channel_id"])
            if ch is not None:
                try:
                    await ch.get_partial_message(post["message_id"]).delete()
                except discord.HTTPException:
                    pass
            self.db.delete_porch_post(post["message_id"])
        elif self.db.porch_post(payload.message_id):
            self.db.delete_porch_post(payload.message_id)

    # ------------------------------------------------------------ buttons
    async def say_hi(self, interaction: discord.Interaction, post: dict) -> None:
        if post["stoop_id"]:
            await self.fresh_card(post["stoop_id"])
            post = self.db.porch_post(post["message_id"]) or post
            if post["state"] in ("missing", "gone"):
                return await tell(interaction, T.PORCH_STALE)
        char, _ = self.char_for_post(post)
        name = (char or {}).get("title") or "this character"
        thread = await self.hi_thread(post, char)
        if thread is None:
            return await tell(interaction, T.THREAD_FAILED)
        await tell(interaction, T.PORCH_HI_DONE.format(name=escape_markdown(name), url=thread.jump_url))
        self.db.log(post["guild_id"], post["message_id"], interaction.user.id, "porch_hi")

    async def hi_thread(self, post: dict, char: Optional[dict]) -> Optional[discord.Thread]:
        """The card's Say-hi thread, created (with an intro) the first time."""
        channel = self.bot.get_channel(post["channel_id"])
        if channel is None:
            return None
        if post["thread_id"]:
            th = channel.guild.get_thread(post["thread_id"])
            if th is None:
                try:
                    th = await self.bot.fetch_channel(post["thread_id"])
                except discord.HTTPException:
                    th = None
                if not isinstance(th, discord.Thread):
                    th = None
            if th is not None:
                return th
        name = (char or {}).get("title") or "this character"
        flow = self.bot.get_cog("SunoFlow")
        msg = channel.get_partial_message(post["message_id"])
        thread = await flow.thread_for_message(msg, T.PORCH_HI_THREAD.format(name=name)) if flow else None
        if thread is None:
            return None
        if not post["thread_id"]:
            by = T.PORCH_HI_BY.format(creator=escape_markdown(char["creator"])) if char and char.get("creator") else ""
            try:
                await thread.send(T.PORCH_HI_INTRO.format(name=escape_markdown(name), by=by),
                                  allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as e:
                log.warning("Couldn't post Say-hi intro: %s", e)
            self.db.update_porch_post(post["message_id"], thread_id=thread.id)
        return thread

    async def met(self, interaction: discord.Interaction, post: dict) -> None:
        char, _ = self.char_for_post(post)
        name = (char or {}).get("title") or "them"
        if not self.db.add_met(card_key(post), post["guild_id"], interaction.user.id):
            return await tell(interaction, T.PORCH_MET_AGAIN.format(name=escape_markdown(name)))
        await tell(interaction, T.PORCH_MET_DONE, view=HowWasItView(self, post["message_id"]))
        self.db.log(post["guild_id"], post["message_id"], interaction.user.id, "porch_met")
        # Every card for this character shows the same count.
        posts = self.db.porch_posts_for_stoop(post["stoop_id"]) if post["stoop_id"] else [post]
        for p in posts:
            if p["guild_id"] == post["guild_id"]:
                await self.render_post(p, stats_only=True, force=True)

    async def post_how_it_went(self, interaction: discord.Interaction, mid: int, text: str) -> None:
        post = self.db.porch_post(mid)
        if not post or post["state"] in ("missing", "gone"):
            return await tell(interaction, T.PORCH_STALE)
        char, _ = self.char_for_post(post)
        name = (char or {}).get("title") or "this character"
        thread = await self.hi_thread(post, char)
        if thread is None:
            return await tell(interaction, T.THREAD_FAILED)
        try:
            await thread.send(T.PORCH_HOW_POST.format(user=escape_markdown(interaction.user.display_name),
                                                      name=escape_markdown(name), text=escape_markdown(text)),
                              allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            return await tell(interaction, T.THREAD_FAILED)
        await tell(interaction, T.PORCH_HOW_DONE.format(name=escape_markdown(name), url=thread.jump_url))

    async def start_collab(self, interaction: discord.Interaction, post: dict) -> None:
        """🤝 Start a collab: the existing collab form, posting a new request card in the collab channel.
        Pop-up buttons must answer with the form first, so only database reads happen here."""
        flow = self.bot.get_cog("SunoFlow")
        cfg = self.db.get_config(post["guild_id"])
        if flow is None or not cfg or not cfg.collab_channel_id:
            return await tell(interaction, T.COLLAB_NO_CHANNEL)
        char, _ = self.char_for_post(post)
        if not char:
            return await tell(interaction, T.PORCH_STALE)
        info = dict(char)
        if post["stoop_id"] and not info.get("id"):
            info["id"] = post["stoop_id"]
        if post["adult_ok"] and not (post["stoop_id"] and info.get("stoop") and not info.get("nsfw")):
            info["nsfw"] = True           # the collab channel is SFW: name + link only
        from suno_flow import CollabModal
        # One share row per collab attempt; the interaction id is a unique, message-id-sized number.
        sid = interaction.id
        self.db.create_share(sid, post["guild_id"], post["channel_id"], interaction.user.id,
                             interaction.user.display_name, [info["url"]] if info.get("url") else [], kind="porch")
        self.db.update_share(sid, songs=[info], origin_url=jump(post["guild_id"], post["channel_id"], post["message_id"]))
        await interaction.response.send_modal(CollabModal(flow, self.db.get_share(sid)))

    async def report(self, interaction: discord.Interaction, mid: int, reason: str, details: Optional[str]) -> None:
        post = self.db.porch_post(mid)
        if not post:
            return await tell(interaction, T.PORCH_CARD_GONE)
        char, _ = self.char_for_post(post)
        name = (char or {}).get("title") or "Unknown character"
        self.db.add_report(post["guild_id"], mid, card_key(post), interaction.user.id, reason, details)
        reason_label = next((f"{e} {label}" for v, label, e in T.PORCH_REPORT_REASONS if v == reason), reason)
        lines = [T.PORCH_REPORT_HEADER,
                 f"**Character:** {escape_markdown(name)}" + (" 🔞" if (char or {}).get("nsfw") else ""),
                 f"**Card:** {jump(post['guild_id'], post['channel_id'], mid)}"]
        if (char or {}).get("url"):
            lines.append(f"**Link:** <{char['url']}>")
        if post["poster_id"]:
            lines.append(f"**Shared by:** <@{post['poster_id']}>")
        lines.append(f"**Reason:** {reason_label}")
        if details:
            lines.append("**Details:** " + short(escape_markdown(details), 800))
        lines.append(f"**Reported by:** {interaction.user.mention}")
        sent = await self.alert_mods(post["guild_id"], "\n".join(lines))
        await tell(interaction, T.PORCH_REPORT_DONE if sent else T.PORCH_REPORT_NO_CHANNEL)
        self.db.log(post["guild_id"], mid, interaction.user.id, "porch_report")

    async def alert_mods(self, guild_id: int, text: str) -> bool:
        cfg = self.db.get_config(guild_id)
        ch = self.bot.get_channel(cfg.mod_channel_id) if cfg and cfg.mod_channel_id else None
        if ch is None:
            log.info("Mod alert (no mod channel set): %s", text.splitlines()[0])
            return False
        try:
            await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
            return True
        except discord.HTTPException as e:
            log.warning("Couldn't send mod alert: %s", e)
            return False

    # ------------------------------------------------------------ rendering existing cards
    async def render_post(self, post: dict, *, stats_only: bool = False, force: bool = False,
                          state: Optional[str] = None) -> None:
        """Bring one porch card up to date.
        stats_only: edit the embed only (keep the attached art). force: edit even if recently edited."""
        channel = self.bot.get_channel(post["channel_id"])
        if channel is None:
            return
        char, stoop = self.char_for_post(post)
        rec = self.db.stoop_card(post["stoop_id"]) if post["stoop_id"] else None
        adult_ok = bool(post["adult_ok"]) and bool(getattr(channel, "is_nsfw", lambda: False)())
        if state is None:
            if rec and rec["status"] in ("missing", "gone"):
                state = rec["status"]
            elif char:
                state = desired_state(char, adult_ok)
            else:
                state = "gone"
        if stats_only and state != post["state"]:
            stats_only = False
        if stats_only and not force and (post["rendered_at"] or 0) > time.time() - STATS_EDIT_EVERY:
            return

        cfg = self.db.get_config(post["guild_id"])
        files: list[discord.File] = []
        image_name = None
        asset = (char or {}).get("stoop_asset") if state == "full" else None
        if state == "full" and asset:
            if stats_only and post["shown_asset"] == asset:
                image_name = f"stoop_{''.join(c for c in asset if c.isalnum() or c == '-')[:8]}.webp"
            else:
                f = await self.image_file(asset)
                if f:
                    files, image_name = [f], f.filename
                    stats_only = False
        content, embed, view = render_card(
            char or {}, state=state, kind=post["kind"], message_id=post["message_id"],
            met=self.db.met_count(card_key(post), post["guild_id"]), stoop=stoop,
            poster_name=self._poster_name(channel, post), image_name=image_name,
            porch18_id=cfg.porch18_channel_id if cfg else None, big_image=post["kind"] == "arrival")
        kwargs = {"content": content, "embed": embed, "view": view}
        if not stats_only:
            kwargs["attachments"] = files          # replaces (or removes) the art
        try:
            await channel.get_partial_message(post["message_id"]).edit(**kwargs)
        except discord.NotFound:
            self.db.delete_porch_post(post["message_id"])
            return
        except discord.HTTPException as e:
            log.warning("Couldn't update porch card: %s", e)
            return
        self.db.update_porch_post(post["message_id"], state=state, rendered_at=time.time(),
                                  shown_version=(stoop or {}).get("version"),
                                  shown_asset=asset if files or (stats_only and image_name) else None)

    @staticmethod
    def _poster_name(channel, post: dict) -> Optional[str]:
        if not post["poster_id"]:
            return None
        member = channel.guild.get_member(post["poster_id"]) if getattr(channel, "guild", None) else None
        return member.display_name if member else "A Dreamer"

    # ------------------------------------------------------------ syncing with The Stoop
    async def apply_card(self, card: dict) -> None:
        """New data for a card (from a poll, a detail check, or a member's link). Updates what the bot shows."""
        cid = card["id"]
        old = self.db.stoop_card(cid)
        self.db.save_stoop_card(card)
        if old is None:
            return
        restored = old["status"] in ("missing", "gone")
        version_up = (card.get("version") or 0) > (old["version"] or 0)
        rating_changed = bool(old["nsfw"]) != bool(card.get("nsfw"))
        old_data = old["data"] or {}
        looks_changed = any(old_data.get(k) != card.get(k) for k in ("name", "summary", "tags", "primaryAssetId"))
        content_changed = restored or version_up or rating_changed or looks_changed
        if old_data.get("primaryAssetId") and old_data.get("primaryAssetId") != card.get("primaryAssetId"):
            self.forget_image(old_data.get("primaryAssetId"))
        await self.refresh_everywhere(cid, content_changed=content_changed,
                                      version_note=(old["version"], card.get("version"))
                                      if version_up and not restored and old["version"] else None)

    async def refresh_everywhere(self, cid: str, *, content_changed: bool,
                                 version_note: Optional[tuple] = None) -> None:
        rec = self.db.stoop_card(cid)
        card = rec["data"] if rec else None
        for post in self.db.porch_posts_for_stoop(cid):
            await self.render_post(post, stats_only=not content_changed, force=content_changed)
            if version_note and post["thread_id"] and card and not (card.get("nsfw") and not post["adult_ok"]):
                cfg = self.db.get_config(post["guild_id"])
                if cfg and cfg.stoop_notes:
                    await self._thread_note(post, T.PORCH_UPDATE_NOTE.format(
                        name=escape_markdown(card.get("name") or "This character"), old=version_note[0],
                        new=version_note[1], url=jump(post["guild_id"], post["channel_id"], post["message_id"])))
        if card and card.get("nsfw"):
            await self.handle_refs(cid, "adult")

    async def _thread_note(self, post: dict, text: str) -> None:
        th = self.bot.get_channel(post["thread_id"])
        if th is None:
            try:
                th = await self.bot.fetch_channel(post["thread_id"])
            except discord.HTTPException:
                return
        try:
            await th.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    async def on_missing(self, cid: str) -> None:
        """The API says 404: hide it everywhere right away (it may be 18+ now, or removed)."""
        rec = self.db.stoop_card(cid)
        if rec and rec["status"] != "live":
            return
        self.db.mark_stoop_missing(cid)
        for post in self.db.porch_posts_for_stoop(cid):
            await self.render_post(post, state="missing", force=True)
        await self.handle_refs(cid, "missing")
        log.info("Stoop card %s is no longer available; hidden", cid)

    async def on_gone(self, cid: str) -> None:
        """Still 404 after GONE_AFTER: final tombstone, forget the content."""
        rec = self.db.stoop_card(cid)
        name = (rec["data"] or {}).get("name") if rec and rec["data"] else None
        asset = (rec["data"] or {}).get("primaryAssetId") if rec and rec["data"] else None
        self.db.mark_stoop_gone(cid)
        self.forget_image(asset)
        for post in self.db.porch_posts_for_stoop(cid):
            await self.render_post(post, state="gone", force=True)
            if post["thread_id"]:
                await self._thread_note(post, T.PORCH_GONE_THREAD.format(name=escape_markdown(name or "This character")))
        log.info("Stoop card %s tombstoned after %d days unavailable", cid, GONE_AFTER // 86400)

    async def handle_refs(self, cid: str, why: str) -> None:
        """Collab cards and members' posts that show this card: strip / alert when it's missing or now 18+."""
        for ref in self.db.stoop_refs_for(cid):
            if ref["adult_ok"] or ref["state"] != "ok":
                continue
            if ref["kind"] == "collab_card":
                await self._strip_collab_card(ref, why)
                self.db.set_stoop_ref_state(ref["message_id"], cid, "stripped")
            elif ref["kind"] == "member_post":
                url = jump(ref["guild_id"], ref["channel_id"], ref["message_id"])
                text = (T.MOD_ALERT_ADULT if why == "adult" else T.MOD_ALERT_MISSING).format(url=url)
                await self.alert_mods(ref["guild_id"], text)
                self.db.set_stoop_ref_state(ref["message_id"], cid, "alerted")

    async def _strip_collab_card(self, ref: dict, why: str) -> None:
        channel = self.bot.get_channel(ref["channel_id"])
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(ref["message_id"])
        except discord.HTTPException:
            return
        if not msg.embeds:
            return
        old = msg.embeds[0]
        embed = discord.Embed(title=old.title, url=old.url, color=old.color)
        if old.author and old.author.name:
            embed.set_author(name=old.author.name, icon_url=old.author.icon_url)
        drop = {T.CHARACTER_ABOUT_FIELD, T.CHARACTER_TAGS_FIELD, T.COLLAB_IDEA_FIELD}
        for f in old.fields:
            if f.name not in drop:
                embed.add_field(name=f.name, value=f.value, inline=f.inline)
        note = T.CHARACTER_ADULT_NOTE if why == "adult" else T.COLLAB_CHARACTER_UNAVAILABLE
        embed.add_field(name=T.CHARACTER_ABOUT_FIELD, value=note, inline=False)
        try:
            await msg.edit(embed=embed, attachments=[])
        except discord.HTTPException as e:
            log.warning("Couldn't strip collab card: %s", e)

    # ------------------------------------------------------------ age restriction changes
    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after) -> None:
        """The 18+ porch lost (or regained) its age restriction: bring its cards in line right away."""
        try:
            if getattr(before, "is_nsfw", None) is None or before.is_nsfw() == after.is_nsfw():
                return
            for post in self.db.porch_posts_in_channel(after.id):
                await self.render_post(post, force=True, stats_only=False)
        except Exception:
            log.exception("Porch: channel update handling failed")

    async def sweep_adult_channels(self) -> None:
        """Safety net: full 18+ cards in a channel that isn't age-restricted (any more) get stripped."""
        for post in self.db.adult_porch_posts():
            ch = self.bot.get_channel(post["channel_id"])
            if ch is not None and not ch.is_nsfw():
                char, _ = self.char_for_post(post)
                if char and char.get("nsfw"):
                    await self.render_post(post, force=True)

    # ------------------------------------------------------------ polling
    def _feeds_wanted(self) -> dict[str, bool]:
        cfgs = [c for c in self.db.all_configs() if c]
        known = bool(self.db.stoop_known_ids())
        return {
            "sfw": known or any(c.stoop_feed and c.porch_channel_id for c in cfgs),
            "nsfw": known or any(c.stoop_feed18 and c.porch18_channel_id for c in cfgs),
        }

    async def poll_once(self) -> None:
        """Both since-polls (SFW and 18+), then post pending arrivals. Safe to call any time."""
        if not self.client.enabled or self.client.unauthorized:
            return
        async with self._poll_lock:
            wanted = self._feeds_wanted()
            for rating in ("sfw", "nsfw"):
                if not wanted[rating]:
                    continue
                key = f"stoop_since_{rating}"
                since = self.db.kv_get(key)
                t = stoop_api.parse_time(since)
                if not since or t is None or (time.time() - t.timestamp()) > STALE_SINCE:
                    self.db.kv_set(key, stoop_api.now_iso())       # start from now: no backlog dump
                    continue
                try:
                    cards, complete = await self.client.changes_since(rating=rating, since=since)
                except (StoopRateLimited, StoopUnauthorized):
                    return
                except StoopError as e:
                    log.warning("Stoop %s poll failed: %s", rating, e)
                    continue
                for card in sorted(cards, key=lambda c: c.get("createdAt") or ""):
                    await self.on_listed(card, rating)
                if not complete:
                    # Too many changes for one poll: keep `since` so the older-created ones aren't skipped.
                    log.warning("Stoop %s poll incomplete; keeping the watermark and trying again next poll", rating)
                    continue
                newest = max([since] + [stoop_api.changed_at(c) for c in cards])
                self.db.kv_set(key, newest)              # the boundary card will come again; that's fine
            await self.announce_pending()

    async def on_listed(self, card: dict, rating: str) -> None:
        """A card from a since-poll."""
        if not card.get("id"):
            return
        # The filter already did this, but never trust a list for rating: check the flag.
        if rating == "sfw" and card.get("nsfw"):
            log.warning("SFW poll returned an 18+ card; it will only ever be shown as 18+")
        if self.db.stoop_card(card["id"]) is not None:
            await self.apply_card(card)              # known: edits, comebacks, rating changes
            return
        if self._arrival_candidate(card):
            self.db.save_stoop_card(card)           # announce_pending posts it

    def _arrival_candidate(self, card: dict) -> bool:
        created = card.get("createdAt") or ""
        for c in self.db.all_configs():
            if not c:
                continue
            if not card.get("nsfw") and c.stoop_feed and c.porch_channel_id and created >= (c.stoop_feed_since or "9"):
                return True
            if card.get("nsfw") and c.stoop_feed18 and c.porch18_channel_id and created >= (c.stoop_feed18_since or "9"):
                return True
        return False

    async def announce_pending(self) -> None:
        for cfg in self.db.all_configs():
            if not cfg:
                continue
            channels = self.porch_channels(cfg)
            if cfg.stoop_feed and cfg.porch_channel_id and cfg.stoop_feed_since:
                await self._announce(cfg, cfg.porch_channel_id, False, cfg.stoop_feed_since)
            if cfg.stoop_feed18 and cfg.porch18_channel_id and cfg.stoop_feed18_since:
                if channels.get(cfg.porch18_channel_id):          # must be age-restricted right now
                    await self._announce(cfg, cfg.porch18_channel_id, True, cfg.stoop_feed18_since)
                else:
                    log.warning("18+ feed skipped: the 18+ porch isn't marked age-restricted in Discord")

    async def _announce(self, cfg: GuildConfig, channel_id: int, nsfw: bool, since_iso: str) -> None:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return
        for rec in self.db.stoop_pending_arrivals(channel_id, nsfw, since_iso, MAX_ARRIVALS_PER_POLL):
            card = rec["data"]
            if bool(card.get("nsfw")) != nsfw:
                continue
            adult_ok = nsfw          # 18+ arrivals only ever go to the age-restricted porch
            char = stoop_api.to_character_info(card)
            state = desired_state(char, adult_ok)
            if state != "full":
                continue
            f = await self.image_file(char.get("stoop_asset"))
            content, embed, _ = render_card(char, state=state, kind="arrival", message_id=0, met=0, stoop=card,
                                            image_name=f.filename if f else None, porch18_id=cfg.porch18_channel_id)
            try:
                msg = await channel.send(content, embed=embed, files=[f] if f else [],
                                         allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as e:
                log.warning("Couldn't post arrival: %s", e)
                return
            # Track it before anything else can fail, so it's never orphaned (or posted twice).
            self.db.add_porch_post(msg.id, cfg.guild_id, channel_id, "arrival", adult_ok=adult_ok,
                                   stoop_id=card["id"], state=state, shown_version=card.get("version"),
                                   shown_asset=char.get("stoop_asset") if f else None)
            self.db.log(cfg.guild_id, msg.id, None, "porch_arrival")
            _, _, view = render_card(char, state=state, kind="arrival", message_id=msg.id, met=0, stoop=card)
            try:
                await msg.edit(view=view)
            except discord.HTTPException as e:
                log.warning("Couldn't add arrival buttons: %s", e)

    async def recheck_once(self) -> None:
        """Detail-check cards the bot shows, to notice removals. Tombstone long-missing ones."""
        if not self.client.enabled or self.client.unauthorized:
            return
        now = time.time()
        for cid in self.db.stoop_cards_due(now, RECENT_DAYS, RECENT_EVERY, OLD_EVERY, RECHECK_PER_CYCLE):
            try:
                card = await self.client.card(cid)
            except (StoopRateLimited, StoopUnauthorized):
                return
            except StoopError as e:
                log.warning("Stoop re-check failed: %s", e)
                return
            if card:
                await self.apply_card(card)
            elif (self.db.stoop_card(cid) or {}).get("status") == "missing":
                self.db.touch_stoop_card(cid)            # still missing; next check in a day
            else:
                await self.on_missing(cid)
        for cid in self.db.stoop_cards_missing_since(now - GONE_AFTER):
            await self.on_gone(cid)
        await self.sweep_adult_channels()

    async def stoop_stats(self) -> Optional[dict]:
        """/partner/stats, cached for 10 minutes."""
        t, data = self._stats_cache
        if data is not None and time.time() - t < 600:
            return data
        if not self.client.enabled:
            return None
        try:
            data = await asyncio.wait_for(self.client.stats(), 10)
        except (StoopError, asyncio.TimeoutError):
            return None
        self._stats_cache = (time.time(), data)
        return data

    @tasks.loop(minutes=FEED_MINUTES)
    async def feed_loop(self) -> None:
        try:
            await self.poll_once()
        except Exception:
            log.exception("Stoop feed poll failed (will retry)")

    @tasks.loop(minutes=RECHECK_MINUTES)
    async def recheck_loop(self) -> None:
        try:
            await self.recheck_once()
        except Exception:
            log.exception("Stoop re-check failed (will retry)")

    @feed_loop.before_loop
    @recheck_loop.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Porch(bot))
