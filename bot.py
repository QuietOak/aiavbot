# AIAVBOT - a Discord bot for the Dreamers AI Hub community
# Copyright (C) 2026 Oak
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU Affero General Public License v3.0 or later. See LICENSE.
"""
bot.py - AIAVBOT entry point.

Run:  python bot.py
Settings come from a .env file next to this script (see .env.example).
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import socket
import sys
import time
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

from storage import Storage

HERE = Path(__file__).resolve().parent


def data_dir() -> Path:
    """A folder outside Dropbox for the database, log and (optionally) the .env with the token."""
    if sys.platform == "win32" and os.getenv("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "AIAVBOT"
    return Path.home() / ".aiavbot"


# .env is read from the data folder first (safest), then from next to this script.
load_dotenv(data_dir() / ".env")
load_dotenv(HERE / ".env")


def default_db_path() -> Path:
    return data_dir() / "aiavbot.db"


_lock_handle = None


def ensure_single_instance(folder: Path) -> bool:
    """Only one copy of the bot may run. Two copies both answer every click and trip each other up."""
    global _lock_handle
    _lock_handle = open(folder / "aiavbot.lock", "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            _lock_handle.seek(0)
            msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def setup_logging(db_dir: Path) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    fileh = logging.handlers.RotatingFileHandler(
        db_dir / "aiavbot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    fileh.setFormatter(fmt)
    root.addHandler(fileh)
    logging.getLogger("discord.http").setLevel(logging.WARNING)


class AIAVBot(commands.Bot):
    def __init__(self, db: Storage, dev_guild_id: int | None):
        intents = discord.Intents.default()
        intents.message_content = True  # needed to read Suno links in messages
        super().__init__(
            command_prefix=commands.when_mentioned,  # no text commands; slash commands only
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
            help_command=None,
        )
        self.db = db
        self.dev_guild_id = dev_guild_id

    async def setup_hook(self) -> None:
        await self.load_extension("suno_flow")
        await self.load_extension("admin")
        await self.load_extension("nightly")
        await self.load_extension("reactions")

        if self.dev_guild_id:
            # Test server: commands appear instantly.
            guild = discord.Object(id=self.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logging.info("Synced %d commands to dev guild %s", len(synced), self.dev_guild_id)
        else:
            synced = await self.tree.sync()
            logging.info("Synced %d global commands", len(synced))

        # Keep references so these background tasks can't be garbage-collected.
        self._bg_tasks = [asyncio.create_task(self._watch_loop_lag()),
                          asyncio.create_task(self._keep_connection_warm())]

    async def login(self, token: str) -> None:
        """Use a connection to Discord that stays ready between clicks.

        discord.py's default forgets Discord's address after 10 s and closes idle connections after 15 s,
        so the first click after a quiet spell has to look up and reconnect before it can answer. On some
        PCs that takes several seconds, longer than Discord's 3-second limit."""
        family = socket.AF_INET if os.getenv("FORCE_IPV4", "1") != "0" else socket.AF_UNSPEC
        self.http.connector = aiohttp.TCPConnector(
            limit=0, ttl_dns_cache=600, keepalive_timeout=120, family=family)
        await super().login(token)

    async def _keep_connection_warm(self) -> None:
        """Tiny request every 45 s so the connection never goes cold. Logs if Discord is slow to answer."""
        from discord.http import Route
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(45)
            start = time.monotonic()
            try:
                await self.http.request(Route("GET", "/users/@me"))
            except Exception as e:
                logging.warning("Connection check to Discord failed: %r", e)
                continue
            took = time.monotonic() - start
            if took > 1.5:
                logging.warning("Slow connection to Discord: a tiny request took %.1fs", took)

    async def on_ready(self) -> None:
        logging.info("Logged in as %s (id %s) in %d server(s)", self.user, self.user.id, len(self.guilds))

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Diagnostics: Discord only allows 3 seconds to answer a click or command."""
        lag = (discord.utils.utcnow() - interaction.created_at).total_seconds()
        if lag > 1.5:
            logging.warning(
                "Interaction reached the bot %.1fs after it was made (limit 3s). If this keeps happening, "
                "check the PC's clock is synced (Windows Settings > Time > Sync now) and the network.", lag)

    async def _watch_loop_lag(self) -> None:
        """Diagnostics: warn if the bot freezes (it can't answer anything while frozen)."""
        while not self.is_closed():
            start = time.monotonic()
            await asyncio.sleep(1)
            stall = time.monotonic() - start - 1
            if stall > 1.0:
                logging.warning("Bot was unresponsive for %.1fs", stall)


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("DISCORD_TOKEN is missing. Copy .env.example to .env and paste your bot token in.")
        sys.exit(1)

    db_path = Path(os.getenv("DB_PATH") or default_db_path())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    data_dir().mkdir(parents=True, exist_ok=True)
    if not ensure_single_instance(data_dir()):
        print("AIAVBOT is already running in another window. Close that one first.")
        input("Press Enter to exit.")
        sys.exit(1)
    setup_logging(db_path.parent)
    logging.info("Database: %s", db_path)

    dev_guild = os.getenv("DEV_GUILD_ID")
    bot = AIAVBot(Storage(db_path), int(dev_guild) if dev_guild else None)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
