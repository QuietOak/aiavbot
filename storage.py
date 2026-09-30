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
    -- porch / Stoop columns are added by _migrate()
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

-- Small settings shared by the whole instance (e.g. the Stoop feed watermark).
CREATE TABLE IF NOT EXISTS kv (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

-- Stoop cards the bot knows about (only ones it has shown somewhere).
CREATE TABLE IF NOT EXISTS stoop_cards (
    card_id       TEXT PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'live',   -- live | missing | gone
    nsfw          INTEGER NOT NULL DEFAULT 0,
    version       INTEGER,
    created_at    TEXT,                          -- Stoop createdAt (ISO)
    updated_at    TEXT,                          -- Stoop updatedAt (ISO)
    data          TEXT,                          -- last card JSON; cleared when gone
    fetched_at    REAL,                          -- last time the API confirmed it (list or detail)
    missing_since REAL,
    first_seen    REAL NOT NULL
);

-- The bot's character cards in the porch channels.
CREATE TABLE IF NOT EXISTS porch_posts (
    message_id    INTEGER PRIMARY KEY,           -- the bot's card
    guild_id      INTEGER NOT NULL,
    channel_id    INTEGER NOT NULL,
    kind          TEXT NOT NULL,                 -- arrival (feed) | share (a member's post)
    adult_ok      INTEGER NOT NULL DEFAULT 0,    -- posted in an age-restricted channel
    post_id       INTEGER,                       -- the member's message (share)
    poster_id     INTEGER,                       -- the member who shared it (share)
    stoop_id      TEXT,                          -- Stoop card id, if it's a Stoop character
    info          TEXT,                          -- character dict (JSON) for non-Stoop characters
    thread_id     INTEGER,                       -- the card's conversation thread
    state         TEXT NOT NULL DEFAULT 'full',  -- full | adult | missing | gone
    shown_version INTEGER,
    shown_asset   TEXT,
    rendered_at   REAL,
    created_at    REAL NOT NULL
);

-- Other messages that show or link a Stoop card: collab request cards and members' own posts.
CREATE TABLE IF NOT EXISTS stoop_refs (
    message_id  INTEGER NOT NULL,
    stoop_id    TEXT NOT NULL,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    kind        TEXT NOT NULL,                   -- collab_card | member_post
    adult_ok    INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'ok',      -- ok | stripped | alerted
    created_at  REAL NOT NULL,
    PRIMARY KEY (message_id, stoop_id)
);

CREATE TABLE IF NOT EXISTS porch_met (
    card_key    TEXT NOT NULL,                   -- Stoop id, or "m<message id>" for other characters
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (card_key, guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS porch_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    card_key    TEXT,
    reporter_id INTEGER NOT NULL,
    reason      TEXT,
    details     TEXT,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_porch_stoop ON porch_posts (stoop_id);
CREATE INDEX IF NOT EXISTS idx_refs_stoop ON stoop_refs (stoop_id);
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
    porch_channel_id: Optional[int] = None
    porch18_channel_id: Optional[int] = None
    mod_channel_id: Optional[int] = None
    stoop_feed: int = 0              # SFW arrival feed in the SFW porch
    stoop_feed18: int = 0            # 18+ arrival feed in the age-restricted porch
    stoop_notes: int = 1             # "✨ got an update" notes in conversation threads
    stoop_reports: int = 1           # also send 🚩 reports about Stoop characters to The Stoop's moderators
    showcase_channel_id: Optional[int] = None   # multimedia gallery: every presented collab is mirrored here
    stoop_feed_since: Optional[str] = None     # ISO time the SFW feed was turned on (no backlog before it)
    stoop_feed18_since: Optional[str] = None


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
    origin_url: Optional[str] = None   # porch collabs: link to the porch card they came from

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
        for col, ddl in (("porch_channel_id", "INTEGER"), ("porch18_channel_id", "INTEGER"),
                         ("mod_channel_id", "INTEGER"), ("stoop_feed", "INTEGER NOT NULL DEFAULT 0"),
                         ("stoop_feed18", "INTEGER NOT NULL DEFAULT 0"), ("stoop_notes", "INTEGER NOT NULL DEFAULT 1"),
                         ("stoop_feed_since", "TEXT"), ("stoop_feed18_since", "TEXT"),
                         ("stoop_reports", "INTEGER NOT NULL DEFAULT 1"), ("showcase_channel_id", "INTEGER")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE guild_config ADD COLUMN {col} {ddl}")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(collab_requests)")}
        if "title" not in cols:
            self.conn.execute("ALTER TABLE collab_requests ADD COLUMN title TEXT")
        if "thread_id" not in cols:
            self.conn.execute("ALTER TABLE collab_requests ADD COLUMN thread_id INTEGER")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(gallery_entries)")}
        if "mirror_id" not in cols:
            self.conn.execute("ALTER TABLE gallery_entries ADD COLUMN mirror_id INTEGER")
        if "mirror_channel_id" not in cols:
            self.conn.execute("ALTER TABLE gallery_entries ADD COLUMN mirror_channel_id INTEGER")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(porch_reports)")}
        if "stoop_status" not in cols:
            self.conn.execute("ALTER TABLE porch_reports ADD COLUMN stoop_status TEXT")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(shares)")}
        if "kind" not in cols:
            self.conn.execute("ALTER TABLE shares ADD COLUMN kind TEXT NOT NULL DEFAULT 'music'")
        if "origin_url" not in cols:
            self.conn.execute("ALTER TABLE shares ADD COLUMN origin_url TEXT")
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
        # Ignore columns this version doesn't know (e.g. after rolling the code back), instead of crashing.
        return GuildConfig(**{k: v for k, v in d.items() if k in GuildConfig.__dataclass_fields__})

    def set_mod_roles(self, guild_id: int, role_ids: list[int]) -> None:
        self._exec("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
        self._exec("UPDATE guild_config SET mod_role_ids=? WHERE guild_id=?",
                   (json.dumps(sorted(set(role_ids))), guild_id))

    def set_channels(self, guild_id: int, **channels: Optional[int]) -> GuildConfig:
        """Set any of music/collab/lounge/gallery/porch/porch18/mod channel ids. None = unchanged, 0 = clear."""
        self._exec("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
        for key, value in channels.items():
            if value is not None:
                col = f"{key}_channel_id"
                if col not in ("music_channel_id", "collab_channel_id", "lounge_channel_id", "gallery_channel_id",
                               "porch_channel_id", "porch18_channel_id", "mod_channel_id",
                               "showcase_channel_id"):
                    raise ValueError(key)
                self._exec(f"UPDATE guild_config SET {col}=? WHERE guild_id=?", (value or None, guild_id))
        return self.get_config(guild_id)

    def set_stoop_settings(self, guild_id: int, **fields) -> None:
        """stoop_feed, stoop_feed18, stoop_notes, stoop_feed_since, stoop_feed18_since."""
        allowed = {"stoop_feed", "stoop_feed18", "stoop_notes", "stoop_feed_since", "stoop_feed18_since",
                   "stoop_reports"}
        self._exec("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(k)
            self._exec(f"UPDATE guild_config SET {k}=? WHERE guild_id=?",
                       (int(v) if isinstance(v, bool) else v, guild_id))

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
        return Share(**{k: v for k, v in d.items() if k in Share.__dataclass_fields__})

    def update_share(self, post_id: int, **fields) -> None:
        allowed = {"reply_id", "songs", "selected", "status", "line", "season", "thread_id", "opened_at",
                   "origin_url"}
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
                   note: Optional[str], season: Optional[str], title: Optional[str] = None,
                   thread_id: Optional[int] = None) -> None:
        self._exec(
            """INSERT INTO collab_requests
               (card_id, guild_id, channel_id, post_id, song_idx, creator_id, song_id, types, note, season,
                created_at, title, thread_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (card_id, guild_id, channel_id, post_id, song_idx, creator_id, song_id,
             json.dumps(types), note, season, time.time(), title, thread_id),
        )

    def recent_collabs(self, guild_id: int, since: float, limit: int = 25) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM collab_requests WHERE guild_id=? AND created_at>=? ORDER BY created_at DESC LIMIT ?",
            (guild_id, since, limit)).fetchall()

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

    def set_gallery_mirror(self, card_id: int, mirror_id: int, mirror_channel_id: int) -> None:
        self._exec("UPDATE gallery_entries SET mirror_id=?, mirror_channel_id=? WHERE card_id=?",
                   (mirror_id, mirror_channel_id, card_id))

    def gallery_entry(self, card_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM gallery_entries WHERE card_id=?", (card_id,)).fetchone()

    def gallery_entry_for_post(self, post_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM gallery_entries WHERE post_id=? ORDER BY created_at DESC",
                                 (post_id,)).fetchone()

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
               UNION ALL SELECT creator_id FROM gallery_entries WHERE card_id=? OR mirror_id=?
               UNION ALL SELECT poster_id FROM porch_posts WHERE message_id=? AND poster_id IS NOT NULL""",
            (message_id, message_id, message_id, message_id, message_id),
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

    # ------------------------------------------------------------------ kv
    def kv_get(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: Optional[str]) -> None:
        self._exec("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                   (key, value))

    # ------------------------------------------------------------ stoop cards
    def stoop_card(self, card_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM stoop_cards WHERE card_id=?", (card_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["data"] = json.loads(d["data"]) if d["data"] else None
        return d

    def save_stoop_card(self, card: dict) -> None:
        """Store the latest API data for a card and mark it live."""
        self._exec(
            """INSERT INTO stoop_cards (card_id, status, nsfw, version, created_at, updated_at, data,
                                        fetched_at, missing_since, first_seen)
               VALUES (?, 'live', ?, ?, ?, ?, ?, ?, NULL, ?)
               ON CONFLICT (card_id) DO UPDATE SET status='live', nsfw=excluded.nsfw, version=excluded.version,
                   created_at=excluded.created_at, updated_at=excluded.updated_at, data=excluded.data,
                   fetched_at=excluded.fetched_at, missing_since=NULL""",
            (card["id"], int(bool(card.get("nsfw"))), card.get("version"), card.get("createdAt"),
             card.get("updatedAt"), json.dumps(card, ensure_ascii=False), time.time(), time.time()),
        )

    def mark_stoop_missing(self, card_id: str) -> None:
        self._exec("""UPDATE stoop_cards SET status='missing', fetched_at=?,
                      missing_since=COALESCE(missing_since, ?) WHERE card_id=? AND status='live'""",
                   (time.time(), time.time(), card_id))

    def mark_stoop_gone(self, card_id: str) -> None:
        """Tombstone: forget the card's content, keep the id and status."""
        self._exec("UPDATE stoop_cards SET status='gone', data=NULL WHERE card_id=?", (card_id,))

    def touch_stoop_card(self, card_id: str) -> None:
        self._exec("UPDATE stoop_cards SET fetched_at=? WHERE card_id=?", (time.time(), card_id))

    def stoop_cards_due(self, now: float, recent_days: int, recent_every: float, old_every: float,
                        limit: int, missing_every: float = 86400) -> list[str]:
        """Cards shown somewhere whose last API confirmation is older than their re-check interval.
        Live: every `recent_every` if shown in the last `recent_days`, else `old_every`.
        Missing: every `missing_every` (so a comeback the feed missed is still noticed before the tombstone)."""
        recent_cutoff = now - recent_days * 86400
        rows = self.conn.execute(
            """SELECT c.card_id, c.status, c.fetched_at,
                      MAX(COALESCE((SELECT MAX(p.created_at) FROM porch_posts p WHERE p.stoop_id = c.card_id), 0),
                          COALESCE((SELECT MAX(r.created_at) FROM stoop_refs r WHERE r.stoop_id = c.card_id), 0)) AS shown
               FROM stoop_cards c
               WHERE c.status IN ('live', 'missing')
               AND (EXISTS (SELECT 1 FROM porch_posts p WHERE p.stoop_id = c.card_id)
                    OR EXISTS (SELECT 1 FROM stoop_refs r WHERE r.stoop_id = c.card_id))""").fetchall()
        due = []
        for r in rows:
            if r["status"] == "missing":
                every = missing_every
            else:
                every = recent_every if (r["shown"] or 0) >= recent_cutoff else old_every
            if (r["fetched_at"] or 0) + every <= now:
                due.append((r["fetched_at"] or 0, r["card_id"]))
        return [cid for _, cid in sorted(due)[:limit]]

    def note_stoop_pending(self, card_id: str) -> None:
        """A card we showed without API data (the API was down): re-check it as soon as possible."""
        self._exec("""INSERT INTO stoop_cards (card_id, status, fetched_at, first_seen)
                      VALUES (?, 'live', 0, ?) ON CONFLICT (card_id) DO NOTHING""", (card_id, time.time()))

    def porch_posts_in_channel(self, channel_id: int) -> list[dict]:
        return [self._porch(r) for r in self.conn.execute(
            "SELECT * FROM porch_posts WHERE channel_id=?", (channel_id,))]

    def all_porch_posts(self) -> list[dict]:
        return [self._porch(r) for r in self.conn.execute("SELECT * FROM porch_posts ORDER BY created_at")]

    def adult_porch_posts(self) -> list[dict]:
        return [self._porch(r) for r in self.conn.execute(
            "SELECT * FROM porch_posts WHERE adult_ok=1 AND state='full'")]

    def stoop_cards_missing_since(self, before: float) -> list[str]:
        return [r["card_id"] for r in self.conn.execute(
            "SELECT card_id FROM stoop_cards WHERE status='missing' AND missing_since < ?", (before,))]

    def stoop_known_ids(self) -> set[str]:
        return {r["card_id"] for r in self.conn.execute("SELECT card_id FROM stoop_cards")}

    # ------------------------------------------------------------ porch posts
    def add_porch_post(self, message_id: int, guild_id: int, channel_id: int, kind: str, *, adult_ok: bool,
                       post_id: Optional[int] = None, poster_id: Optional[int] = None,
                       stoop_id: Optional[str] = None, info: Optional[dict] = None, state: str = "full",
                       shown_version: Optional[int] = None, shown_asset: Optional[str] = None) -> None:
        self._exec(
            """INSERT OR REPLACE INTO porch_posts
               (message_id, guild_id, channel_id, kind, adult_ok, post_id, poster_id, stoop_id, info, state,
                shown_version, shown_asset, rendered_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (message_id, guild_id, channel_id, kind, int(adult_ok), post_id, poster_id, stoop_id,
             json.dumps(info, ensure_ascii=False) if info else None, state, shown_version, shown_asset,
             time.time(), time.time()),
        )

    def porch_post(self, message_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM porch_posts WHERE message_id=?", (message_id,)).fetchone()
        return self._porch(row) if row else None

    def porch_post_for_member_post(self, post_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM porch_posts WHERE post_id=?", (post_id,)).fetchone()
        return self._porch(row) if row else None

    def porch_posts_for_stoop(self, stoop_id: str) -> list[dict]:
        return [self._porch(r) for r in self.conn.execute(
            "SELECT * FROM porch_posts WHERE stoop_id=? ORDER BY created_at", (stoop_id,))]

    def porch_post_in_channel(self, stoop_id: str, channel_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM porch_posts WHERE stoop_id=? AND channel_id=? ORDER BY created_at LIMIT 1",
            (stoop_id, channel_id)).fetchone()
        return self._porch(row) if row else None

    def update_porch_post(self, message_id: int, **fields) -> None:
        allowed = {"thread_id", "state", "shown_version", "shown_asset", "rendered_at", "info"}
        for k in fields:
            if k not in allowed:
                raise ValueError(k)
        if "info" in fields and fields["info"] is not None:
            fields["info"] = json.dumps(fields["info"], ensure_ascii=False)
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE porch_posts SET {sets} WHERE message_id=?", (*fields.values(), message_id))

    def delete_porch_post(self, message_id: int) -> None:
        self._exec("DELETE FROM porch_posts WHERE message_id=?", (message_id,))

    @staticmethod
    def _porch(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["info"] = json.loads(d["info"]) if d["info"] else None
        return d

    # ------------------------------------------------------------ stoop refs
    def add_stoop_ref(self, message_id: int, stoop_id: str, guild_id: int, channel_id: int, kind: str,
                      adult_ok: bool, state: str = "ok") -> None:
        self._exec(
            """INSERT OR IGNORE INTO stoop_refs (message_id, stoop_id, guild_id, channel_id, kind, adult_ok,
                                                 state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (message_id, stoop_id, guild_id, channel_id, kind, int(adult_ok), state, time.time()))

    def stoop_refs_for(self, stoop_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM stoop_refs WHERE stoop_id=? ORDER BY created_at", (stoop_id,))]

    def set_stoop_ref_state(self, message_id: int, stoop_id: str, state: str) -> None:
        self._exec("UPDATE stoop_refs SET state=? WHERE message_id=? AND stoop_id=?", (state, message_id, stoop_id))

    # ------------------------------------------------------------ met / reports
    def add_met(self, card_key: str, guild_id: int, user_id: int) -> bool:
        cur = self._exec("INSERT OR IGNORE INTO porch_met (card_key, guild_id, user_id, created_at) VALUES (?, ?, ?, ?)",
                         (card_key, guild_id, user_id, time.time()))
        return cur.rowcount > 0

    def met_count(self, card_key: str, guild_id: int) -> int:
        row = self.conn.execute("SELECT COUNT(*) n FROM porch_met WHERE card_key=? AND guild_id=?",
                                (card_key, guild_id)).fetchone()
        return row["n"] if row else 0

    def add_report(self, guild_id: int, message_id: int, card_key: Optional[str], reporter_id: int,
                   reason: str, details: Optional[str]) -> int:
        cur = self._exec(
            """INSERT INTO porch_reports (guild_id, message_id, card_key, reporter_id, reason, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, message_id, card_key, reporter_id, reason, details, time.time()))
        return cur.lastrowid

    def report_exists(self, card_key: str, guild_id: int, reporter_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM porch_reports WHERE card_key=? AND guild_id=? AND reporter_id=? LIMIT 1",
            (card_key, guild_id, reporter_id)).fetchone() is not None

    def reports_since(self, guild_id: int, reporter_id: int, since: float) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM porch_reports WHERE guild_id=? AND reporter_id=? AND created_at>=?",
            (guild_id, reporter_id, since)).fetchone()
        return row["n"] if row else 0

    def set_report_stoop_status(self, report_id: int, status: str) -> None:
        self._exec("UPDATE porch_reports SET stoop_status=? WHERE id=?", (status, report_id))

    def porch_counts(self) -> dict:
        row = self.conn.execute(
            """SELECT (SELECT COUNT(*) FROM stoop_cards WHERE status='live') AS live,
                      (SELECT COUNT(*) FROM stoop_cards WHERE status='missing') AS missing,
                      (SELECT COUNT(*) FROM stoop_cards WHERE status='gone') AS gone,
                      (SELECT COUNT(*) FROM porch_posts) AS posts""").fetchone()
        return dict(row)

    def note_stoop_unavailable(self, card_id: str) -> None:
        """A card we were asked about but the API says isn't available (e.g. in review). Remembered so a
        later comeback in the change feed restores whatever the bot showed for it."""
        self._exec("""INSERT INTO stoop_cards (card_id, status, fetched_at, missing_since, first_seen)
                      VALUES (?, 'missing', ?, ?, ?) ON CONFLICT (card_id) DO NOTHING""",
                   (card_id, time.time(), time.time(), time.time()))

    def stoop_pending_arrivals(self, channel_id: int, nsfw: bool, since_iso: str, limit: int) -> list[dict]:
        """Live cards created after the feed was turned on that haven't been posted in this channel yet."""
        rows = self.conn.execute(
            """SELECT * FROM stoop_cards c
               WHERE c.status='live' AND c.nsfw=? AND c.created_at >= ? AND c.data IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM porch_posts p WHERE p.stoop_id=c.card_id AND p.channel_id=?)
               ORDER BY c.created_at LIMIT ?""", (int(nsfw), since_iso, channel_id, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"]) if d["data"] else None
            out.append(d)
        return out
