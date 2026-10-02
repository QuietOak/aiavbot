# AIAVBOT - a Discord bot for the Dreamers AI Hub community
# Copyright (C) 2026 Oak
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU Affero General Public License v3.0 or later. See LICENSE.
"""
admin.py - Staff slash commands (/aiav ...).

Who can use them (checked on every command, whatever Discord's menu shows):
  - Administrators and members with Manage Server, always
  - Members with a mod role added via /aiav modrole add

Regular members don't see /aiav at all: by default it's only shown to members with
Manage Messages. If a mod role lacks that permission, the server owner can show the commands
to that role in Server Settings > Integrations > AIAVBOT.
Adding and removing mod roles is limited to Administrators / Manage Server.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

import suno_fetch
import texts as T
from nightly import period_stats, stats_line
from storage import Storage

log = logging.getLogger("aiavbot.admin")

NEEDED_PERMS = {
    "view_channel": "View Channel",
    "send_messages": "Send Messages",
    "embed_links": "Embed Links",
    "attach_files": "Attach Files",
    "read_message_history": "Read Message History",
    "create_public_threads": "Create Public Threads",
    "send_messages_in_threads": "Send Messages in Threads",
}
CUSTOM_EMOJI_RE = re.compile(r"^<a?:\w{2,32}:\d{15,25}>$")


MOD_PERMS = {
    "view_channel": "View Channel",
    "send_messages": "Send Messages",
}

THEME_PERMS = {
    "view_channel": "View Channel",
    "send_messages": "Send Messages / Create Posts",
    "send_messages_in_threads": "Send Messages in Threads",
    "embed_links": "Embed Links",
}


def missing_perms(channel, needed: dict = NEEDED_PERMS) -> list[str]:
    perms = channel.permissions_for(channel.guild.me)
    return [label for attr, label in needed.items() if not getattr(perms, attr)]


def target_kind(ch) -> str:
    if isinstance(ch, discord.ForumChannel):
        return "forum (each song becomes a new post)"
    if isinstance(ch, discord.Thread):
        return "thread"
    return "channel"


async def reply(interaction: discord.Interaction, text: str, ephemeral: bool = True) -> None:
    """Private reply that works whether or not we've already acknowledged the command."""
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


def valid_emoji(text: str) -> bool:
    text = text.strip()
    if CUSTOM_EMOJI_RE.match(text):
        return True
    # Unicode emoji: short and no plain letters/digits.
    return 0 < len(text) <= 8 and not any(c.isascii() and c.isalnum() for c in text)


def fmt_channel(channel_id: Optional[int]) -> str:
    return f"<#{channel_id}>" if channel_id else "*not set*"


NOT_STAFF = "Only mods and admins can change AIAVBOT settings."
NOT_ADMIN = "Only admins (Administrator or Manage Server) can change who counts as a mod."


def is_admin(member: discord.Member) -> bool:
    p = member.guild_permissions
    return p.administrator or p.manage_guild


class NotStaff(app_commands.CheckFailure):
    pass


class NotAdmin(app_commands.CheckFailure):
    pass


def admins_only():
    def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
            raise NotAdmin()
        return True
    return app_commands.check(predicate)


@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
class AIAVAdmin(commands.GroupCog, group_name="aiav", group_description="AIAVBOT settings (mods and admins)"):
    theme = app_commands.Group(name="theme", description="Themes members can share matching songs to")
    modrole = app_commands.Group(name="modrole", description="Roles that may use /aiav (admins only)")
    stoop = app_commands.Group(name="stoop", description="The Stoop library: backlog, backfill and skipped characters")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db: Storage = bot.db  # type: ignore[attr-defined]
        super().__init__()

    # ------------------------------------------------------------ permissions
    def is_staff(self, member: discord.Member) -> bool:
        if is_admin(member):
            return True
        cfg = self.db.get_config(member.guild.id)
        mod_roles = set(cfg.mod_role_ids) if cfg else set()
        return any(r.id in mod_roles for r in member.roles)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Runs before every /aiav command."""
        if not isinstance(interaction.user, discord.Member) or not self.is_staff(interaction.user):
            raise NotStaff()
        # Acknowledge right away so Discord's 3-second limit can't run out while we work.
        if interaction.type == discord.InteractionType.application_command and not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.NotFound:
                log.warning("/aiav: reply window expired before the bot could answer")
        return True

    async def cog_app_command_error(self, interaction: discord.Interaction,
                                    error: app_commands.AppCommandError) -> None:
        if isinstance(error, NotAdmin):
            msg = NOT_ADMIN
        elif isinstance(error, (NotStaff, app_commands.CheckFailure)):
            msg = NOT_STAFF
        else:
            log.exception("Error in /%s", interaction.command.qualified_name if interaction.command else "aiav",
                          exc_info=error)
            msg = "Something went wrong running that command. Details are in the bot's log."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await reply(interaction, msg, ephemeral=True)
        except discord.HTTPException:
            pass

    # --------------------------------------------------------------- mod roles
    @modrole.command(name="add", description="Let a role use /aiav commands")
    @admins_only()
    async def modrole_add(self, interaction: discord.Interaction, role: discord.Role) -> None:
        cfg = self.db.get_config(interaction.guild_id)
        roles = set(cfg.mod_role_ids) if cfg else set()
        roles.add(role.id)
        self.db.set_mod_roles(interaction.guild_id, list(roles))
        await reply(interaction, 
            f"{role.mention} can now use /aiav commands.\n"
            "If they can't see the commands, show them to this role in "
            "Server Settings → Integrations → AIAVBOT.", ephemeral=True)

    @modrole.command(name="remove", description="Stop a role from using /aiav commands")
    @admins_only()
    async def modrole_remove(self, interaction: discord.Interaction, role: discord.Role) -> None:
        cfg = self.db.get_config(interaction.guild_id)
        roles = set(cfg.mod_role_ids) if cfg else set()
        if role.id not in roles:
            return await reply(interaction, f"{role.mention} wasn't a mod role.", ephemeral=True)
        roles.discard(role.id)
        self.db.set_mod_roles(interaction.guild_id, list(roles))
        await reply(interaction, f"{role.mention} can no longer use /aiav commands.", ephemeral=True)

    @modrole.command(name="list", description="Show who can use /aiav commands")
    async def modrole_list(self, interaction: discord.Interaction) -> None:
        cfg = self.db.get_config(interaction.guild_id)
        roles = [f"<@&{rid}>" for rid in (cfg.mod_role_ids if cfg else [])]
        await reply(interaction, 
            "**Who can use /aiav**\n• Administrators and members with Manage Server (always)\n"
            + ("• Mod roles: " + ", ".join(roles) if roles else "• No mod roles yet. Add one with `/aiav modrole add`."),
            ephemeral=True)

    # ------------------------------------------------------------------ setup
    @app_commands.command(name="setup", description="Choose the channels AIAVBOT works in")
    @app_commands.describe(
        music="Channel where Suno links get a reply (the multimedia music channel)",
        collab="Channel where collab requests are posted",
        lounge="AIAV Club lounge: gets the nightly update",
        gallery="AIAV Club gallery: posts get asked 'Is this a collab result?'",
        porch="SFW character porch: character shares + the Stoop arrival feed",
        porch_18="18+ character porch. Must be an age-restricted channel",
        mod_alerts="Private mod channel for 🚩 character reports and Stoop heads-ups",
        showcase="Multimedia gallery: every presented collab is copied here too",
    )
    async def setup_cmd(self, interaction: discord.Interaction,
                        music: Optional[discord.TextChannel] = None,
                        collab: Optional[discord.TextChannel] = None,
                        lounge: Optional[discord.TextChannel] = None,
                        gallery: Optional[discord.TextChannel] = None,
                        porch: Optional[discord.TextChannel] = None,
                        porch_18: Optional[discord.TextChannel] = None,
                        mod_alerts: Optional[discord.TextChannel] = None,
                        showcase: Optional[discord.TextChannel] = None) -> None:
        problems = []
        if porch_18 is not None and not porch_18.is_nsfw():
            problems.append(f"⛔ {porch_18.mention} isn't age-restricted, so it can't be the 18+ porch. "
                            "Turn on **Age-Restricted Channel** in its settings, then run this again.")
            porch_18 = None
        if porch is not None and porch_18 is not None and porch.id == porch_18.id:
            problems.append("⛔ The SFW porch and the 18+ porch must be different channels.")
            porch_18 = None
        cfg = self.db.set_channels(
            interaction.guild_id,
            music=music.id if music else None, collab=collab.id if collab else None,
            lounge=lounge.id if lounge else None, gallery=gallery.id if gallery else None,
            porch=porch.id if porch else None, porch18=porch_18.id if porch_18 else None,
            mod=mod_alerts.id if mod_alerts else None, showcase=showcase.id if showcase else None,
        )
        lines = problems + [
            "**AIAVBOT channels**",
            f"🎵 Music: {fmt_channel(cfg.music_channel_id)}",
            f"🤝 Collab requests: {fmt_channel(cfg.collab_channel_id)}",
            f"💬 Lounge: {fmt_channel(cfg.lounge_channel_id)}",
            f"🖼️ Gallery: {fmt_channel(cfg.gallery_channel_id)} → mirrored to {fmt_channel(cfg.showcase_channel_id)}",
            f"🎭 Porch: {fmt_channel(cfg.porch_channel_id)} · 🔞 18+ porch: {fmt_channel(cfg.porch18_channel_id)}",
            f"🛡️ Mod alerts: {fmt_channel(cfg.mod_channel_id)}",
        ]
        lines += self._perm_warnings(interaction.guild, cfg)
        await reply(interaction, "\n".join(lines), ephemeral=True)

    def _perm_warnings(self, guild: discord.Guild, cfg) -> list[str]:
        out = []
        for label, cid in (("Music", cfg.music_channel_id), ("Collab requests", cfg.collab_channel_id),
                           ("Gallery", cfg.gallery_channel_id), ("Lounge", cfg.lounge_channel_id),
                           ("Porch", cfg.porch_channel_id), ("18+ porch", cfg.porch18_channel_id),
                           ("Mod alerts", cfg.mod_channel_id), ("Multimedia gallery", cfg.showcase_channel_id)):
            ch = guild.get_channel(cid) if cid else None
            if ch is None:
                continue
            miss = missing_perms(ch, MOD_PERMS if cid == cfg.mod_channel_id else NEEDED_PERMS)
            if miss:
                out.append(f"⚠️ In {ch.mention} I'm missing: {', '.join(miss)}")
        if cfg.porch18_channel_id:
            ch = guild.get_channel(cfg.porch18_channel_id)
            if ch is not None and not ch.is_nsfw():
                out.append(f"⛔ {ch.mention} is no longer age-restricted: 18+ cards there are shown as 🔞-only "
                           "and the 18+ feed is paused until it's age-restricted again.")
        if not out and cfg.music_channel_id and cfg.collab_channel_id:
            out.append("✅ Permissions look good.")
        return out

    # --------------------------------------------------------------- settings
    @app_commands.command(name="settings", description="Adjust AIAVBOT behaviour")
    @app_commands.describe(
        reply_timeout="Minutes before an unused bot prompt is removed (default 15)",
        nightly_update="Post the midnight update in the lounge (default on)",
        reaction_milestones="Cheer posts that reach 3, 10, 20, 30, 50 reactions (default on)",
        milestone_channels="Where reactions are counted: AIAV channels only (default) or every channel",
        stoop_feed="Post new SFW Stoop characters in the porch (default off)",
        stoop_feed_18="Post new 18+ Stoop characters in the 18+ porch (default off)",
        stoop_update_notes="Note character updates (v2 → v3) in their conversation threads (default on)",
        stoop_reports="Also send 🚩 reports about Stoop characters to The Stoop's moderators (default on)",
    )
    @app_commands.choices(milestone_channels=[
        app_commands.Choice(name="AIAV channels + theme channels", value="aiav"),
        app_commands.Choice(name="Every channel", value="all"),
    ])
    async def settings_cmd(self, interaction: discord.Interaction,
                           reply_timeout: Optional[app_commands.Range[int, 1, 1440]] = None,
                           nightly_update: Optional[bool] = None,
                           reaction_milestones: Optional[bool] = None,
                           milestone_channels: Optional[app_commands.Choice[str]] = None,
                           stoop_feed: Optional[bool] = None,
                           stoop_feed_18: Optional[bool] = None,
                           stoop_update_notes: Optional[bool] = None,
                           stoop_reports: Optional[bool] = None) -> None:
        import stoop_api
        before = self.db.get_config(interaction.guild_id)
        if stoop_feed is not None:
            fields = {"stoop_feed": stoop_feed}
            if stoop_feed and not (before and before.stoop_feed):
                fields["stoop_feed_since"] = stoop_api.now_iso()     # only characters from now on
            self.db.set_stoop_settings(interaction.guild_id, **fields)
        if stoop_feed_18 is not None:
            fields = {"stoop_feed18": stoop_feed_18}
            if stoop_feed_18 and not (before and before.stoop_feed18):
                fields["stoop_feed18_since"] = stoop_api.now_iso()
            self.db.set_stoop_settings(interaction.guild_id, **fields)
        if stoop_update_notes is not None:
            self.db.set_stoop_settings(interaction.guild_id, stoop_notes=stoop_update_notes)
        if stoop_reports is not None:
            self.db.set_stoop_settings(interaction.guild_id, stoop_reports=stoop_reports)
        if stoop_feed or stoop_feed_18:
            # Start the change feed at the same moment, so nothing created in the next few minutes is missed.
            for rating in ("sfw", "nsfw"):
                if not self.db.kv_get(f"stoop_since_{rating}"):
                    self.db.kv_set(f"stoop_since_{rating}", stoop_api.now_iso())
        if reaction_milestones is not None or milestone_channels is not None:
            self.db.set_milestone_settings(interaction.guild_id, enabled=reaction_milestones,
                                           scope=milestone_channels.value if milestone_channels else None)
        if reply_timeout is not None:
            self.db.set_reply_timeout(interaction.guild_id, reply_timeout)
        if nightly_update is not None:
            self.db.set_nightly(interaction.guild_id, enabled=nightly_update)
        cfg = self.db.get_config(interaction.guild_id)
        timeout = cfg.reply_timeout_min if cfg else 15
        nightly = "on" if (cfg is None or cfg.nightly_update) else "off"
        lounge = "" if cfg and cfg.lounge_channel_id else " (set a lounge with `/aiav setup lounge:`)"
        milestones = "on" if (cfg is None or cfg.milestones) else "off"
        scope = "every channel" if cfg and cfg.milestone_scope == "all" else "AIAV + theme channels"
        feed = "on" if cfg and cfg.stoop_feed else "off"
        feed18 = "on" if cfg and cfg.stoop_feed18 else "off"
        notes = "on" if (cfg is None or cfg.stoop_notes) else "off"
        sreports = "on" if (cfg is None or cfg.stoop_reports) else "off"
        porch_hint = "" if cfg and cfg.porch_channel_id else " (set a porch with `/aiav setup porch:`)"
        await reply(interaction,
            f"⏱️ Unused prompts are shortened (music) or removed (collab/gallery) after **{timeout} min**.\n"
            f"🌙 Nightly lounge update: **{nightly}**{lounge}\n"
            f"🏆 Reaction milestones: **{milestones}** ({scope})\n"
            f"🎭 Stoop arrival feed: **{feed}**{porch_hint} · 18+ feed: **{feed18}** · update notes: **{notes}**\n"
            f"🚩 Reports also go to The Stoop's moderators: **{sreports}**")

    @app_commands.command(name="gallery_report", description="Who was involved in AIAV gallery publications in a month")
    @app_commands.describe(month="Which month, as YYYY-MM (default: last month)",
                           public="Post the list in this channel instead of only showing it to you")
    async def gallery_report_cmd(self, interaction: discord.Interaction, month: Optional[str] = None,
                                 public: bool = False) -> None:
        import gallery_report as G
        cfg = self.db.get_config(interaction.guild_id)
        channel = interaction.guild.get_channel(cfg.gallery_channel_id) if cfg and cfg.gallery_channel_id else None
        if channel is None:
            return await reply(interaction, "Set the gallery first with `/aiav setup gallery:`.")
        try:
            start, end, label = G.month_bounds(month)
        except (ValueError, TypeError):
            return await reply(interaction, "Please give the month as YYYY-MM, e.g. 2026-09.")
        try:
            pubs, counts, names = await G.count_gallery(self.db, interaction.guild, channel, start, end,
                                                        self.bot.user.id if self.bot.user else None)
        except discord.HTTPException as e:
            log.warning("Gallery report failed: %s", e)
            return await reply(interaction, f"I couldn't read {channel.mention}. Check I have Read Message History there.")
        if not pubs:
            return await reply(interaction, T.REPORT_EMPTY.format(channel=channel.mention, month=label))
        lines = G.format_report(label, pubs, counts, names, T)
        chunks, chunk = [], ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 1900:
                chunks.append(chunk)
                chunk = ""
            chunk += line + "\n"
        chunks.append(chunk)
        for c in chunks:
            if public and interaction.channel is not None:
                await interaction.channel.send(c, allowed_mentions=discord.AllowedMentions.none())
            else:
                await interaction.followup.send(c, ephemeral=True)
        if public:
            await reply(interaction, "Posted. ✅")

    @app_commands.command(name="update", description="Post an AIAV Club activity update in the lounge now")
    async def update_cmd(self, interaction: discord.Interaction) -> None:
        nightly_cog = self.bot.get_cog("Nightly")
        msg = await nightly_cog.post_update(interaction.guild_id, nightly=False) if nightly_cog else None
        if msg:
            await reply(interaction, f"Posted: {msg.jump_url}")
        else:
            await reply(interaction, "Couldn't post. Check the lounge is set (`/aiav setup lounge:`) "
                                     "and `/aiav status` for missing permissions.")

    # ----------------------------------------------------------------- status
    @app_commands.command(name="status", description="Show AIAVBOT's setup and recent activity")
    async def status_cmd(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        cfg = self.db.get_config(guild.id)
        if not cfg:
            return await interaction.followup.send("Not set up yet. Run `/aiav setup` first.", ephemeral=True)

        label = getattr(self.bot, "instance_label", None)
        lines = [
            "**AIAVBOT status**" + (f"  ·  🧩 `{label}`" if label else ""),
            f"🎵 Music: {fmt_channel(cfg.music_channel_id)}",
            f"🤝 Collab requests: {fmt_channel(cfg.collab_channel_id)}",
            f"🖼️ Gallery: {fmt_channel(cfg.gallery_channel_id)} → mirrored to {fmt_channel(cfg.showcase_channel_id)}",
            f"💬 Lounge: {fmt_channel(cfg.lounge_channel_id)} · nightly update "
            + ("on" if cfg.nightly_update else "off"),
            f"⏱️ Unused prompts tidied after {cfg.reply_timeout_min} min",
            "🏆 Reaction milestones: " + ("on" if cfg.milestones else "off")
            + (" (every channel)" if cfg.milestone_scope == "all" else " (AIAV + theme channels)"),
        ]
        lines += self._perm_warnings(guild, cfg)

        themes = self.db.active_seasons(guild.id)
        if themes:
            for t in themes:
                lines.append(f"🏷️ Theme **{t.label}** → {self._target_text(guild, t.target_id)}")
        else:
            lines.append("🏷️ Themes: *none active* (the theme button is hidden)")

        lines.append(f"🌐 Suno: {await self._suno_check()}")
        lines += await self._porch_status(cfg)

        lines.append("📊 **Activity**")
        from datetime import datetime
        for label, stats in period_stats(self.db, guild.id, datetime.now().astimezone()):
            lines.append(f"**{label}:** {stats_line(stats)}")
            if cfg.porch_channel_id or cfg.porch18_channel_id:
                lines.append(f"    {T.PORCH_STAT_TITLE}: {stats_line(stats, T.PORCH_STAT_LABELS)}")
        # Discord messages max out at 2000 characters: send in chunks of whole lines.
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 1900:
                await interaction.followup.send(chunk, ephemeral=True)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            await interaction.followup.send(chunk, ephemeral=True)

    async def _porch_status(self, cfg) -> list[str]:
        porch = self.bot.get_cog("Porch")
        lines = [f"🎭 Porch: {fmt_channel(cfg.porch_channel_id)} · feed " + ("on" if cfg.stoop_feed else "off")
                 + f"  ·  🔞 18+ porch: {fmt_channel(cfg.porch18_channel_id)} · feed "
                 + ("on" if cfg.stoop_feed18 else "off"),
                 f"🛡️ Mod alerts: {fmt_channel(cfg.mod_channel_id)} · reports to The Stoop: "
                 + ("on" if cfg.stoop_reports else "off")]
        if porch is None:
            return lines + ["🏡 The Stoop: porch module not loaded"]
        client = porch.client
        if not client.enabled:
            return lines + ["🏡 The Stoop API: not configured (add STOOP_API_KEY to the env file)"]
        stats = await porch.stoop_stats()
        health = client.health()
        icon = "✅" if health in ("ok", "not used yet") and stats is not None else "⚠️"
        text = f"🏡 The Stoop API: {icon} {'working' if stats is not None else health}"
        cards = (stats or {}).get("cards") if isinstance((stats or {}).get("cards"), dict) else None
        if cards and isinstance(cards.get("total"), int):
            nsfw = cards.get("nsfw") if isinstance(cards.get("nsfw"), int) else 0
            text += f" · {cards['total']:,} characters ({cards['total'] - nsfw:,} SFW, {nsfw:,} 18+)"
        for cid in (cfg.porch_channel_id, cfg.porch18_channel_id):
            line = self._backfill_line(self.db.backfill(cfg.guild_id, cid)) if cid else ""
            if line:
                text += f"\n<#{cid}>" + line
        counts = self.db.porch_counts()
        text += (f"\n    tracking {counts['live']} live · {counts['missing']} unavailable · {counts['gone']} gone"
                 f" · {client.requests_last_hour()} API calls in the last hour")
        return lines + [text]

    async def _suno_check(self) -> str:
        row = self.db.conn.execute(
            "SELECT links FROM shares WHERE links LIKE '%suno.%' ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            return "no songs seen yet"
        import json
        link = next((u for u in json.loads(row["links"]) if "suno." in u), None)
        if not link:
            return "no songs seen yet"
        try:
            song = await asyncio.wait_for(suno_fetch.fetch_song(link), 10)
        except Exception as e:
            return f"⚠️ couldn't reach Suno ({type(e).__name__})"
        if song.source == "clip_api":
            return "✅ working (full song info)"
        return "⚠️ basic info only: Suno's data feed may have changed"

    # ------------------------------------------------------------------ the Stoop library
    def _porch_ready(self, interaction: discord.Interaction):
        porch = self.bot.get_cog("Porch")
        if porch is None:
            return None, "The porch module isn't loaded."
        if not porch.client.enabled:
            return None, "The Stoop API isn't configured (add STOOP_API_KEY to the env file)."
        return porch, ""

    def _backfill_line(self, job: Optional[dict]) -> str:
        if not job:
            return ""
        if job["status"] == "done":
            return f"    📚 backfill done: {job['posted']} posted"
        hours = job["left"] / max(1, job["per_hour"])
        eta = f"about {hours:.0f} h left" if hours >= 1 else "under an hour left"
        state = "running" if job["status"] == "running" else "paused"
        return (f"    📚 backfill {state}: {job['posted']} posted · {job['left']} to go · "
                f"{job['per_hour']}/hour · {eta}")

    @stoop.command(name="check", description="How many Stoop characters aren't in the porch channels yet")
    async def stoop_check(self, interaction: discord.Interaction) -> None:
        porch, err = self._porch_ready(interaction)
        if not porch:
            return await reply(interaction, err)
        try:
            info = await porch.backlog(interaction.guild_id)
        except Exception as e:
            log.warning("Stoop check failed: %r", e)
            return await reply(interaction, "Couldn't read The Stoop's list right now. Try again in a minute.")
        if not info:
            return await reply(interaction, "Set up the porch channels first: `/aiav setup porch: porch_18:`.")
        lines = ["🏡 **The Stoop library**"]
        for rating, label in (("sfw", "SFW"), ("nsfw", "18+")):
            if rating not in info:
                continue
            i = info[rating]
            lines.append(f"**{label}** → <#{i['channel_id']}>: {i['total']:,} on The Stoop · {i['posted']:,} posted · "
                         f"**{len(i['missing']):,} not posted** · {i['skipped']:,} skipped")
            line = self._backfill_line(self.db.backfill(interaction.guild_id, i["channel_id"]))
            if line:
                lines.append(line)
        lines.append("Post the missing ones with `/aiav stoop backfill`.")
        await reply(interaction, "\n".join(lines))

    @stoop.command(name="backfill", description="Post Stoop characters that aren't in a porch channel yet, oldest first")
    @app_commands.describe(channel="Which porch", per_hour="How many per hour (default 20)",
                           limit="Only this many (the oldest first). Default: all of them")
    @app_commands.choices(channel=[app_commands.Choice(name="SFW porch", value="sfw"),
                                   app_commands.Choice(name="18+ porch", value="nsfw")])
    async def stoop_backfill(self, interaction: discord.Interaction, channel: app_commands.Choice[str],
                             per_hour: app_commands.Range[int, 1, 60] = 20,
                             limit: Optional[app_commands.Range[int, 1, 10000]] = None) -> None:
        porch, err = self._porch_ready(interaction)
        if not porch:
            return await reply(interaction, err)
        cfg = self.db.get_config(interaction.guild_id)
        cid = (cfg.porch_channel_id if channel.value == "sfw" else cfg.porch18_channel_id) if cfg else None
        current = self.db.backfill(interaction.guild_id, cid) if cid else None
        if current and current["status"] == "running":
            return await reply(interaction, "A backfill is already running there:\n" + self._backfill_line(current)
                               + "\nStop it with `/aiav stoop backfill_stop` first to restart it.")
        try:
            job, problem = await porch.start_backfill(interaction.guild_id, channel.value, per_hour, limit,
                                                      interaction.user.id)
        except Exception as e:
            log.warning("Backfill start failed: %r", e)
            return await reply(interaction, "Couldn't read The Stoop's list right now. Try again in a minute.")
        if problem == "not_set":
            return await reply(interaction, f"Set the {channel.name} first with `/aiav setup`.")
        if problem == "not_age_restricted":
            return await reply(interaction, "The 18+ porch must be an age-restricted channel first.")
        if not job["total"]:
            return await reply(interaction, f"Nothing to do: every character is already in <#{job['channel_id']}> "
                                            "(or skipped). ✅")
        hours = job["total"] / per_hour
        await reply(interaction,
            f"📚 Backfill started in <#{job['channel_id']}>: **{job['total']:,} characters**, oldest first, "
            f"{per_hour} an hour (about {hours:.0f} hours). Each is labelled \"From The Stoop's library\".\n"
            "New arrivals still post as usual. Pause any time with `/aiav stoop backfill_stop`; "
            "running `/aiav stoop backfill` again picks up from where it is.")

    @stoop.command(name="backfill_stop", description="Pause the library backfill in a porch channel")
    @app_commands.choices(channel=[app_commands.Choice(name="SFW porch", value="sfw"),
                                   app_commands.Choice(name="18+ porch", value="nsfw")])
    async def stoop_backfill_stop(self, interaction: discord.Interaction, channel: app_commands.Choice[str]) -> None:
        cfg = self.db.get_config(interaction.guild_id)
        cid = (cfg.porch_channel_id if channel.value == "sfw" else cfg.porch18_channel_id) if cfg else None
        job = self.db.backfill(interaction.guild_id, cid) if cid else None
        if not job or job["status"] != "running":
            return await reply(interaction, "No backfill is running there.")
        self.db.set_backfill(interaction.guild_id, cid, status="paused")
        await reply(interaction, "⏸️ Paused.\n" + self._backfill_line(self.db.backfill(interaction.guild_id, cid)))

    @stoop.command(name="skip", description="Never auto-post a Stoop character (or all of one creator's)")
    @app_commands.describe(card="Stoop card link or id", creator="Stoop creator profile link or id",
                           reason="Why (shown in /aiav stoop skipped)",
                           remove_posted="Also remove the bot's cards already posted for it (default yes)")
    async def stoop_skip(self, interaction: discord.Interaction, card: Optional[str] = None,
                         creator: Optional[str] = None, reason: Optional[app_commands.Range[str, 1, 200]] = None,
                         remove_posted: bool = True) -> None:
        import stoop_api
        porch = self.bot.get_cog("Porch")
        if porch is None:
            return await reply(interaction, "The porch module isn't loaded.")
        if bool(card) == bool(creator):
            return await reply(interaction, "Give either a `card:` or a `creator:` (one at a time).")
        if card:
            target = stoop_api.parse_card_ref(card)
            if not target:
                return await reply(interaction, "That doesn't look like a Stoop card link or id.")
            data = await porch.fresh_card(target) if porch.client.enabled else None
            rec = self.db.stoop_card(target)
            label = (data or (rec or {}).get("data") or {}).get("name") or target
            removed = await porch.skip(interaction.guild_id, "card", target, label, reason, interaction.user.id,
                                       remove_posted)
            what = f"**{discord.utils.escape_markdown(label)}**"
        else:
            target = stoop_api.parse_creator_ref(creator)
            if not target:
                return await reply(interaction, "That doesn't look like a Stoop creator link or id.")
            label = self._creator_name(target) or target
            removed = await porch.skip(interaction.guild_id, "creator", target, label, reason, interaction.user.id,
                                       remove_posted)
            what = f"all characters by **{discord.utils.escape_markdown(label)}**"
        extra = f" Removed {removed} posted card{'s' if removed != 1 else ''}." if removed else ""
        await reply(interaction, f"🙈 The bot won't auto-post {what} any more.{extra}\n"
                                 "(Members can still share it themselves. Undo with `/aiav stoop unskip`.)")

    def _creator_name(self, creator_id: str) -> Optional[str]:
        import json as _json
        for r in self.db.conn.execute("SELECT data FROM stoop_cards WHERE data IS NOT NULL"):
            d = _json.loads(r["data"])
            c = d.get("creator") if isinstance(d.get("creator"), dict) else {}
            if c.get("id") == creator_id and c.get("displayName"):
                return c["displayName"]
        return None

    @stoop.command(name="unskip", description="Allow a skipped Stoop character or creator again")
    @app_commands.describe(card="Stoop card link or id", creator="Stoop creator profile link or id")
    async def stoop_unskip(self, interaction: discord.Interaction, card: Optional[str] = None,
                           creator: Optional[str] = None) -> None:
        import stoop_api
        if bool(card) == bool(creator):
            return await reply(interaction, "Give either a `card:` or a `creator:` (one at a time).")
        kind, target = ("card", stoop_api.parse_card_ref(card)) if card else ("creator", stoop_api.parse_creator_ref(creator))
        if target and self.db.remove_skip(interaction.guild_id, kind, target):
            return await reply(interaction, "✅ Un-skipped. It can be posted again (a backfill or a new update brings it in).")
        await reply(interaction, "That wasn't on the skip list.")

    @stoop.command(name="skipped", description="List the Stoop characters and creators the bot won't auto-post")
    async def stoop_skipped(self, interaction: discord.Interaction) -> None:
        rows = self.db.skips(interaction.guild_id)
        if not rows:
            return await reply(interaction, "Nothing is skipped.")
        lines = ["🙈 **Skipped**"]
        for r in rows[:40]:
            who = "🎭" if r["kind"] == "card" else "👤 creator"
            why = f": {r['reason']}" if r["reason"] else ""
            by = f" · by <@{r['by_user']}>" if r["by_user"] else ""
            lines.append(f"{who} **{discord.utils.escape_markdown(r['label'] or r['target_id'])}**{why}{by}")
        if len(rows) > 40:
            lines.append(f"…and {len(rows) - 40} more")
        await reply(interaction, "\n".join(lines)[:1990])

    # ------------------------------------------------------------------ themes
    def _target_text(self, guild: discord.Guild, target_id: Optional[int]) -> str:
        if not target_id:
            return "⚠️ no destination set (re-add it with `where:`)"
        ch = guild.get_channel_or_thread(target_id)
        if ch is None:
            return f"<#{target_id}> ⚠️ can't see it (deleted, archived, or no access)"
        miss = missing_perms(ch, THEME_PERMS)
        warn = f" ⚠️ missing: {', '.join(miss)}" if miss else ""
        return f"{ch.mention} ({target_kind(ch)}){warn}"

    @theme.command(name="add", description="Add (or update) a theme members can share songs to")
    @app_commands.describe(
        name="Theme name, e.g. Spooky Season",
        where="Channel, thread or forum where matching songs get shared",
        emoji="An emoji for the theme, e.g. 🎃",
        description="Short description shown to members (what fits this theme?)",
        ends="Last day of the theme, as YYYY-MM-DD (optional)",
    )
    async def theme_add(self, interaction: discord.Interaction,
                        name: app_commands.Range[str, 1, 60],
                        where: Union[discord.TextChannel, discord.Thread, discord.ForumChannel],
                        emoji: Optional[str] = None,
                        description: Optional[app_commands.Range[str, 1, 100]] = None,
                        ends: Optional[str] = None) -> None:
        if emoji and not valid_emoji(emoji):
            return await reply(interaction,
                "That doesn't look like a single emoji. Try something like 🎃 or a server emoji.")
        if ends:
            try:
                end_date = date.fromisoformat(ends.strip())
            except ValueError:
                return await reply(interaction, "Please give the end date as YYYY-MM-DD, e.g. 2026-10-31.")
            if end_date < date.today():
                return await reply(interaction, "That end date is already past.")
            ends = end_date.isoformat()
        t = self.db.add_season(interaction.guild_id, name.strip(), emoji.strip() if emoji else None,
                               description, ends, where.id)
        until = f" until **{t.ends_at}**" if t.ends_at else ""
        await reply(interaction,
            f"🏷️ **{t.label}** is active{until}.\n"
            f"Songs shared to it go to {self._target_text(interaction.guild, where.id)}.\n"
            "Members now see a share-to-theme button on their song panel.")

    @theme.command(name="end", description="End a theme now")
    @app_commands.describe(name="The theme to end")
    async def theme_end(self, interaction: discord.Interaction, name: str) -> None:
        if self.db.end_season(interaction.guild_id, name):
            left = self.db.active_seasons(interaction.guild_id)
            extra = "" if left else " No themes are active, so the button is hidden."
            await reply(interaction, f"Ended **{name}**.{extra}")
        else:
            await reply(interaction, f"No active theme called **{name}**.")

    @theme_end.autocomplete("name")
    async def _theme_end_ac(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=t.name, value=t.name)
                for t in self.db.all_seasons(interaction.guild_id)
                if t.active and current.lower() in t.name.lower()][:25]

    @theme.command(name="list", description="List themes")
    async def theme_list(self, interaction: discord.Interaction) -> None:
        themes = self.db.all_seasons(interaction.guild_id)
        if not themes:
            return await reply(interaction, "No themes yet. Add one with `/aiav theme add`.")
        active_ids = {t.id for t in self.db.active_seasons(interaction.guild_id)}
        lines = []
        for t in themes[:20]:
            state = "🟢 active" if t.id in active_ids else "⚪ ended"
            until = f" · until {t.ends_at}" if t.ends_at else ""
            desc = f"\n  {t.description}" if t.description else ""
            lines.append(f"{state} **{t.label}**{until} → {self._target_text(interaction.guild, t.target_id)}{desc}")
        await reply(interaction, "\n".join(lines))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AIAVAdmin(bot))
