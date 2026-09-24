# AIAVBOT - a Discord bot for the Dreamers AI Hub community
# Copyright (C) 2026 Oak
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU Affero General Public License v3.0 or later. See LICENSE.
"""
storage.py - SQLite storage for AIAVBOT.

Keep the database OUTSIDE Dropbox (or any sync folder): sync tools can corrupt a
SQLite file that is open. The default location is set in bot.py (DB_PATH in .env).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id            INTEGER PRIMARY KEY,
    music_channel_id    INTEGER,
    collab_channel_id   INTEGER,
    lounge_channel_id   INTEGER,
    gallery_channel_id  INTEGER,
    reply_timeout_min   INTEGER NOT NULL DEFAULT 15,
    mod_role_ids        TEXT,              -- JSON list of role ids allowed to use /aiav
    nightly_update      INTEGER NOT NULL DEFAULT 1,   -- post the midnight update in the lounge
    nightly_last        TEXT,                         -- local date of the last nightly update
    milestones          INTEGER NOT NULL DEFAULT 1,   -- announce reaction milestones
    milestone_scope     TEXT NOT NULL DEFAULT 'aiav'  -- 'aiav' (AIAV channels + theme channels) or 'all'
);

-- Highest reaction milestone announced per message.
CREATE TABLE IF NOT EXISTS reaction_milestones (
    message_id      INTEGER PRIMARY KEY,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    author_id       INTEGER,
    last_milestone  INTEGER NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS seasons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    emoji       TEXT,
    description TEXT,
    ends_at     TEXT,               -- ISO date (YYYY-MM-DD), inclusive; NULL = no end
    active      INTEGER NOT NULL DEFAULT 1,
    target_id   INTEGER,            -- channel, thread or forum where themed songs are shared
    created_at  REAL    NOT NULL,
    UNIQUE (guild_id, name COLLATE NOCASE)
);

-- One row per music post the bot replied to.
CREATE TABLE IF NOT EXISTS shares (
    post_id     INTEGER PRIMARY KEY,   -- the member's message id
    kind        TEXT NOT NULL DEFAULT 'music',   -- music | collab | gallery (which channel it came from)
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    poster_id   INTEGER NOT NULL,
    poster_name TEXT,
    reply_id    INTEGER,               -- the bot's public reply
    links       TEXT NOT NULL,         -- JSON list of Suno links
    songs       TEXT,                  -- JSON list of fetched song dicts (NULL until fetched)
    selected    INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | kept | dismissed | expired
    line        TEXT,                  -- "Add a line about it"
    season      TEXT,                  -- seasonal tag chosen
    thread_id   INTEGER,
    created_at  REAL NOT NULL,
    opened_at   REAL
);

CREATE TABLE IF NOT EXISTS collab_requests (
    card_id     INTEGER PRIMARY KEY,   -- message id of the card in the collab channel
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    post_id     INTEGER NOT NULL,
    song_idx    INTEGER NOT NULL DEFAULT 0,
    creator_id  INTEGER NOT NULL,
    song_id     TEXT,
    types       TEXT,                  -- JSON list
    note        TEXT,
    season      TEXT,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS collab_interest (
    card_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (card_id, user_id)
);

-- Copies of songs shared to a theme's channel/thread/forum.
CREATE TABLE IF NOT EXISTS theme_posts (
    post_id     INTEGER NOT NULL,      -- the member's original message
    season_id   INTEGER NOT NULL,
    song_idx    INTEGER NOT NULL DEFAULT 0,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,      -- where the copy went (channel or thread id)
    message_id  INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (post_id, season_id, song_idx)
);

-- Collab results presented in the gallery.
CREATE TABLE IF NOT EXISTS gallery_entries (
    card_id         INTEGER PRIMARY KEY,   -- the bot's presentation message
    post_id         INTEGER NOT NULL,      -- the member's gallery post
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    thread_id       INTEGER,
    creator_id      INTEGER NOT NULL,
    title           TEXT,
    member_ids      TEXT,                  -- JSON list of collaborator user ids on the server
    others          TEXT,                  -- free-text collaborators (not on the server)
    note            TEXT,
    created_at      REAL NOT NULL
);

-- Library of every Suno song the bot has seen (for "songs like this" later).
CREATE TABLE IF NOT EXISTS songs (
    song_id     TEXT PRIMARY KEY,
    title       TEXT,
    style       TEXT,
    handle      TEXT,
    guild_id    INTEGER,
    poster_id   INTEGER,
    post_id     INTEGER,
    data        TEXT,
    first_seen  REAL NOT NULL
);

-- Simple action log for success signals.
CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER,
    post_id     INTEGER,
    user_id     INTEGER,
    action      TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shares_status ON shares (status, created_at);
CREATE INDEX IF NOT EXISTS idx_collab_post ON collab_requests (post_id, song_idx);
"""


@dataclass
class GuildConfig:
    guild_id: int
    music_channel_id: Optional[int]
    collab_channel_id: Optional[int]
    lounge_channel_id: Optional[int]
    gallery_channel_id: Optional[int]
    reply_timeout_min: int
    mod_role_ids: list
    nightly_update: int = 1
    nightly_last: Optional[str] = None
    milestones: int = 1
    milestone_scope: str = "aiav"


@dataclass
class Season:
    id: int
    guild_id: int
    name: str
    emoji: Optional[str]
    description: Optional[str]
    ends_at: Optional[str]
    active: bool
    target_id: Optional[int] = None

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}" if self.emoji else self.name


@dataclass
class Share:
    post_id: int
    guild_id: int
    channel_id: int
    poster_id: int
    poster_name: Optional[str]
    reply_id: Optional[int]
    links: list
    songs: Optional[list]
    selected: int
    status: str
    line: Optional[str]
    season: Optional[str]
    thread_id: Optional[int]
    created_at: float
    opened_at: Optional[float]
    kind: str = "music"

    @property
    def song(self) -> Optional[dict]:
        """The currently selected song dict, if fetched."""
        if not self.songs:
            return None
        idx = self.selected if 0 <= self.selected < len(self.songs) else 0
        return self.songs[idx]

    @property
    def link(self) -> Optional[str]:
        if not self.links:
            return None
        idx = self.selected if 0 <= self.selected < len(self.links) else 0
        return self.links[idx]

    @property
    def jump_url(self) -> str:
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.post_id}"


def _today() -> str:
    return datetime.now().date().isoformat()  # local date: bot is hosted locally


class Storage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Short timeout: the bot is single-instance, so a lock should never be held for long,
        # and waiting blocks the whole bot.
        self.conn = sqlite3.connect(self.path, timeout=1.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        # Far fewer disk flushes per save (safe with WAL). Keeps saves fast on Windows.
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Bring databases made by older versions up to date."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(guild_config)")}
        if "mod_role_ids" not in cols:
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN mod_role_ids TEXT")
        if "nightly_update" not in cols:
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN nightly_update INTEGER NOT NULL DEFAULT 1")
        if "nightly_last" not in cols:
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN nightly_last TEXT")
        if "milestones" not in cols:
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN milestones INTEGER NOT NULL DEFAULT 1")
        if "milestone_scope" not in cols:
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN milestone_scope TEXT NOT NULL DEFAULT 'aiav'")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(shares)")}
        if "kind" not in cols:
            self.conn.execute("ALTER TABLE shares ADD COLUMN kind TEXT NOT NULL DEFAULT 'music'")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(seasons)")}
        if "target_id" not in cols:
            self.conn.execute("ALTER TABLE seasons ADD COLUMN target_id INTEGER")

    def close(self) -> None:
        self.conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    # ------------------------------------------------------------------ config
    def get_config(self, guild_id: int) -> Optional[GuildConfig]:
        row = self.conn.execute("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["mod_role_ids"] = json.loads(d["mod_role_ids"]) if d.get("mod_role_ids") else []
        return GuildConfig(**d)

    def set_mod_roles(self, guild_id: int, role_ids: list[int]) -> None:
        self._exec("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
        self._exec("UPDATE guild_config SET mod_role_ids=? WHERE guild_id=?",
                   (json.dumps(sorted(set(role_ids))), guild_id))

    def set_channels(self, guild_id: int, **channels: Optional[int]) -> GuildConfig:
        """Set any of music/collab/lounge/gallery channel ids. None values are left unchanged."""
        self._exec("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
        for key, value in channels.items():
            if value is not None:
                col = f"{key}_channel_id"
                if col not in ("music_channel_id", "collab_channel_id", "lounge_channel_id", "gallery_channel_id"):
                    raise ValueError(key)
                self._exec(f"UPDATE guild_config SET {col}=? WHERE guild_id=?", (value, guild_id))
        return self.get_config(guild_id)

    def set_nightly(self, guild_id: int, enabled: Optional[bool] = None, last: Optional[str] = None) -> None:
        self._exec("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
        if enabled is not None:
            self._exec("UPDATE guild_config SET nightly_update=? WHERE guild_id=?", (int(enabled), guild_id))
        if last is not None:
            self._exec("UPDATE guild_config SET nightly_last=? WHERE guild_id=?", (last, guild_id))

    def set_milestone_settings(self, guild_id: int, enabled: Optional[bool] = None,
                               scope: Optional[str] = None) -> None:
        self._exec("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
        if enabled is not None:
            self._exec("UPDATE guild_config SET milestones=? WHERE guild_id=?", (int(enabled), guild_id))
        if scope is not None:
            self._exec("UPDATE guild_config SET milestone_scope=? WHERE guild_id=?", (scope, guild_id))

    def all_configs(self) -> list[GuildConfig]:
        ids = [r["guild_id"] for r in self.conn.execute("SELECT guild_id FROM guild_config")]
        return [self.get_config(g) for g in ids]

    def set_reply_timeout(self, guild_id: int, minutes: int) -> None:
        self._exec("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
        self._exec("UPDATE guild_config SET reply_timeout_min=? WHERE guild_id=?", (minutes, guild_id))

    # ----------------------------------------------------------------- seasons
    def add_season(self, guild_id: int, name: str, emoji: Optional[str],
                   description: Optional[str], ends_at: Optional[str], target_id: int) -> Season:
        """Add a theme, or reactivate/update one with the same name."""
        self._exec(
            """INSERT INTO seasons (guild_id, name, emoji, description, ends_at, active, created_at, target_id)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT (guild_id, name) DO UPDATE SET
                   emoji=excluded.emoji, description=excluded.description,
                   ends_at=excluded.ends_at, active=1, target_id=excluded.target_id""",
            (guild_id, name, emoji, description, ends_at, time.time(), target_id),
        )
        return self.get_season(guild_id, name)

    def get_season(self, guild_id: int, name: str) -> Optional[Season]:
        row = self.conn.execute(
            "SELECT * FROM seasons WHERE guild_id=? AND name=? COLLATE NOCASE", (guild_id, name)
        ).fetchone()
        return self._season(row) if row else None

    def end_season(self, guild_id: int, name: str) -> bool:
        cur = self._exec(
            "UPDATE seasons SET active=0 WHERE guild_id=? AND name=? COLLATE NOCASE AND active=1",
            (guild_id, name),
        )
        return cur.rowcount > 0

    def get_season_by_id(self, season_id: int) -> Optional[Season]:
        row = self.conn.execute("SELECT * FROM seasons WHERE id=?", (season_id,)).fetchone()
        return self._season(row) if row else None

    def active_seasons(self, guild_id: int) -> list[Season]:
        """Themes members can share to right now (active, not past end date, destination set)."""
        rows = self.conn.execute(
            """SELECT * FROM seasons WHERE guild_id=? AND active=1 AND target_id IS NOT NULL
               AND (ends_at IS NULL OR ends_at >= ?) ORDER BY created_at""",
            (guild_id, _today()),
        ).fetchall()
        return [self._season(r) for r in rows]

    def all_seasons(self, guild_id: int) -> list[Season]:
        rows = self.conn.execute(
            "SELECT * FROM seasons WHERE guild_id=? ORDER BY active DESC, created_at DESC", (guild_id,)
        ).fetchall()
        return [self._season(r) for r in rows]

    @staticmethod
    def _season(row: sqlite3.Row) -> Season:
        d = dict(row)
        d.pop("created_at", None)
        d["active"] = bool(d["active"])
        return Season(**d)

    # ------------------------------------------------------------------ shares
    def create_share(self, post_id: int, guild_id: int, channel_id: int, poster_id: int,
                     poster_name: str, links: list[str], kind: str = "music") -> None:
        self._exec(
            """INSERT OR IGNORE INTO shares
               (post_id, guild_id, channel_id, poster_id, poster_name, links, created_at, kind)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (post_id, guild_id, channel_id, poster_id, poster_name, json.dumps(links), time.time(), kind),
        )

    def get_share(self, post_id: int) -> Optional[Share]:
        row = self.conn.execute("SELECT * FROM shares WHERE post_id=?", (post_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["links"] = json.loads(d["links"])
        d["songs"] = json.loads(d["songs"]) if d["songs"] else None
        return Share(**d)

    def update_share(self, post_id: int, **fields) -> None:
        allowed = {"reply_id", "songs", "selected", "status", "line", "season", "thread_id", "opened_at"}
        for k in fields:
            if k not in allowed:
                raise ValueError(k)
        if "songs" in fields and fields["songs"] is not None:
            fields["songs"] = json.dumps(fields["songs"], ensure_ascii=False)
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE shares SET {sets} WHERE post_id=?", (*fields.values(), post_id))

    def expired_shares(self, now: Optional[float] = None) -> list[Share]:
        """Pending shares whose public reply has passed the guild's timeout."""
        now = now or time.time()
        rows = self.conn.execute(
            """SELECT s.post_id FROM shares s
               LEFT JOIN guild_config g ON g.guild_id = s.guild_id
               WHERE s.status='pending' AND s.reply_id IS NOT NULL
               AND MAX(s.created_at, COALESCE(s.opened_at, 0))
                   + COALESCE(g.reply_timeout_min, 15) * 60 < ?""",
            (now,),
        ).fetchall()
        return [self.get_share(r["post_id"]) for r in rows]

    # ------------------------------------------------------------------- songs
    def remember_song(self, song: dict, guild_id: int, poster_id: int, post_id: int) -> None:
        if not song.get("id"):
            return
        self._exec(
            """INSERT INTO songs (song_id, title, style, handle, guild_id, poster_id, post_id, data, first_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (song_id) DO UPDATE SET title=excluded.title, style=excluded.style,
                   data=excluded.data""",
            (song["id"], song.get("title"), song.get("style"), song.get("handle"),
             guild_id, poster_id, post_id, json.dumps(song, ensure_ascii=False), time.time()),
        )

    # ------------------------------------------------------------------ collab
    def add_collab(self, card_id: int, guild_id: int, channel_id: int, post_id: int, song_idx: int,
                   creator_id: int, song_id: Optional[str], types: list[str],
                   note: Optional[str], season: Optional[str]) -> None:
        self._exec(
            """INSERT INTO collab_requests
               (card_id, guild_id, channel_id, post_id, song_idx, creator_id, song_id, types, note, season, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (card_id, guild_id, channel_id, post_id, song_idx, creator_id, song_id,
             json.dumps(types), note, season, time.time()),
        )

    def get_collab_for(self, post_id: int, song_idx: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM collab_requests WHERE post_id=? AND song_idx=? ORDER BY created_at DESC",
            (post_id, song_idx),
        ).fetchone()

    def get_collab(self, card_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM collab_requests WHERE card_id=?", (card_id,)).fetchone()

    def collabs_for_post(self, post_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM collab_requests WHERE post_id=? ORDER BY song_idx", (post_id,)
        ).fetchall()

    def add_interest(self, card_id: int, user_id: int) -> bool:
        """Returns False if this user already said they're interested."""
        cur = self._exec(
            "INSERT OR IGNORE INTO collab_interest (card_id, user_id, created_at) VALUES (?, ?, ?)",
            (card_id, user_id, time.time()),
        )
        return cur.rowcount > 0

    # ------------------------------------------------------------ theme posts
    def add_theme_post(self, post_id: int, season_id: int, song_idx: int, guild_id: int,
                       channel_id: int, message_id: int) -> None:
        self._exec(
            """INSERT OR REPLACE INTO theme_posts
               (post_id, season_id, song_idx, guild_id, channel_id, message_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (post_id, season_id, song_idx, guild_id, channel_id, message_id, time.time()),
        )

    def theme_posts_for(self, post_id: int, song_idx: Optional[int] = None) -> list[sqlite3.Row]:
        """Theme copies of a post, with the theme's name and emoji."""
        sql = """SELECT t.*, s.name, s.emoji FROM theme_posts t JOIN seasons s ON s.id = t.season_id
                 WHERE t.post_id=?"""
        params: tuple = (post_id,)
        if song_idx is not None:
            sql += " AND t.song_idx=?"
            params += (song_idx,)
        return self.conn.execute(sql + " ORDER BY t.created_at", params).fetchall()

    # ----------------------------------------------------------------- actions
    def log(self, guild_id: Optional[int], post_id: Optional[int], user_id: Optional[int], action: str) -> None:
        self._exec(
            "INSERT INTO actions (guild_id, post_id, user_id, action, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, post_id, user_id, action, time.time()),
        )

    def add_gallery_entry(self, card_id: int, post_id: int, guild_id: int, channel_id: int,
                          thread_id: Optional[int], creator_id: int, title: str,
                          member_ids: list[int], others: Optional[str], note: Optional[str]) -> None:
        self._exec(
            """INSERT OR REPLACE INTO gallery_entries
               (card_id, post_id, guild_id, channel_id, thread_id, creator_id, title, member_ids, others, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (card_id, post_id, guild_id, channel_id, thread_id, creator_id, title,
             json.dumps(member_ids), others, note, time.time()),
        )

    # ------------------------------------------------------------- reactions
    def get_milestone(self, message_id: int) -> int:
        row = self.conn.execute("SELECT last_milestone FROM reaction_milestones WHERE message_id=?",
                                (message_id,)).fetchone()
        return row["last_milestone"] if row else 0

    def set_milestone(self, message_id: int, guild_id: int, channel_id: int,
                      author_id: Optional[int], milestone: int) -> None:
        self._exec(
            """INSERT INTO reaction_milestones (message_id, guild_id, channel_id, author_id, last_milestone, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (message_id) DO UPDATE SET last_milestone=excluded.last_milestone,
                   updated_at=excluded.updated_at""",
            (message_id, guild_id, channel_id, author_id, milestone, time.time()),
        )

    def card_owner(self, message_id: int) -> Optional[int]:
        """If a bot message is one of our cards (theme copy, collab request, gallery), who made it."""
        row = self.conn.execute(
            """SELECT s.poster_id AS uid FROM theme_posts t JOIN shares s ON s.post_id = t.post_id
               WHERE t.message_id=?
               UNION ALL SELECT creator_id FROM collab_requests WHERE card_id=?
               UNION ALL SELECT creator_id FROM gallery_entries WHERE card_id=?""",
            (message_id, message_id, message_id),
        ).fetchone()
        return row["uid"] if row else None

    def theme_target_ids(self, guild_id: int) -> set[int]:
        return {t.target_id for t in self.active_seasons(guild_id) if t.target_id}

    def stats_between(self, guild_id: int, start: float, end: float) -> dict:
        rows = self.conn.execute(
            """SELECT action, COUNT(*) n FROM actions
               WHERE guild_id=? AND created_at>=? AND created_at<? GROUP BY action""",
            (guild_id, start, end),
        ).fetchall()
        return {r["action"]: r["n"] for r in rows}

    def stats(self, guild_id: int, since_days: int = 30) -> dict:
        since = time.time() - since_days * 86400
        rows = self.conn.execute(
            "SELECT action, COUNT(*) n FROM actions WHERE guild_id=? AND created_at>=? GROUP BY action",
            (guild_id, since),
        ).fetchall()
        return {r["action"]: r["n"] for r in rows}
