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
    )
    async def setup_cmd(self, interaction: discord.Interaction,
                        music: Optional[discord.TextChannel] = None,
                        collab: Optional[discord.TextChannel] = None,
                        lounge: Optional[discord.TextChannel] = None,
                        gallery: Optional[discord.TextChannel] = None) -> None:
        cfg = self.db.set_channels(
            interaction.guild_id,
            music=music.id if music else None, collab=collab.id if collab else None,
            lounge=lounge.id if lounge else None, gallery=gallery.id if gallery else None,
        )
        lines = [
            "**AIAVBOT channels**",
            f"🎵 Music: {fmt_channel(cfg.music_channel_id)}",
            f"🤝 Collab requests: {fmt_channel(cfg.collab_channel_id)}",
            f"💬 Lounge: {fmt_channel(cfg.lounge_channel_id)}",
            f"🖼️ Gallery: {fmt_channel(cfg.gallery_channel_id)}",
        ]
        lines += self._perm_warnings(interaction.guild, cfg)
        await reply(interaction, "\n".join(lines), ephemeral=True)

    def _perm_warnings(self, guild: discord.Guild, cfg) -> list[str]:
        out = []
        for label, cid in (("Music", cfg.music_channel_id), ("Collab requests", cfg.collab_channel_id),
                           ("Gallery", cfg.gallery_channel_id), ("Lounge", cfg.lounge_channel_id)):
            ch = guild.get_channel(cid) if cid else None
            if ch is None:
                continue
            miss = missing_perms(ch)
            if miss:
                out.append(f"⚠️ In {ch.mention} I'm missing: {', '.join(miss)}")
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
    )
    @app_commands.choices(milestone_channels=[
        app_commands.Choice(name="AIAV channels + theme channels", value="aiav"),
        app_commands.Choice(name="Every channel", value="all"),
    ])
    async def settings_cmd(self, interaction: discord.Interaction,
                           reply_timeout: Optional[app_commands.Range[int, 1, 1440]] = None,
                           nightly_update: Optional[bool] = None,
                           reaction_milestones: Optional[bool] = None,
                           milestone_channels: Optional[app_commands.Choice[str]] = None) -> None:
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
        await reply(interaction,
            f"⏱️ Unused prompts are shortened (music) or removed (collab/gallery) after **{timeout} min**.\n"
            f"🌙 Nightly lounge update: **{nightly}**{lounge}\n"
            f"🏆 Reaction milestones: **{milestones}** ({scope})")

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

        lines = [
            "**AIAVBOT status**",
            f"🎵 Music: {fmt_channel(cfg.music_channel_id)}",
            f"🤝 Collab requests: {fmt_channel(cfg.collab_channel_id)}",
            f"🖼️ Gallery: {fmt_channel(cfg.gallery_channel_id)}",
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

        lines.append("📊 **Activity**")
        from datetime import datetime
        for label, stats in period_stats(self.db, guild.id, datetime.now().astimezone()):
            lines.append(f"**{label}:** {stats_line(stats)}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

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
