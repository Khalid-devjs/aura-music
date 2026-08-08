"""
Async database layer (SQLite via aiosqlite).

Schema:
    users        — every user that interacted with the bot
    groups       — every group the bot was added to
    admins       — extra admins (owner is implicit)
    banned_users — banned user ids
    banned_groups- blacklisted group ids
    settings     — key/value bot settings
    stats        — counters (total_plays, total_commands, ...)
    playlists    — saved queues (owner feature)

The wrapper is deliberately thin so a PostgreSQL/MongoDB backend can be
swapped in later without touching the handlers.
"""
import time

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT DEFAULT '',
    first_name TEXT DEFAULT '',
    is_banned INTEGER DEFAULT 0,
    joined_at INTEGER
);
CREATE TABLE IF NOT EXISTS groups (
    chat_id INTEGER PRIMARY KEY,
    title TEXT DEFAULT '',
    username TEXT DEFAULT '',
    is_blacklisted INTEGER DEFAULT 0,
    streaming_enabled INTEGER DEFAULT 1,
    joined_at INTEGER
);
CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY,
    added_by INTEGER DEFAULT 0,
    added_at INTEGER
);
CREATE TABLE IF NOT EXISTS banned_users (
    user_id INTEGER PRIMARY KEY,
    banned_by INTEGER DEFAULT 0,
    reason TEXT DEFAULT '',
    banned_at INTEGER
);
CREATE TABLE IF NOT EXISTS banned_groups (
    chat_id INTEGER PRIMARY KEY,
    banned_by INTEGER DEFAULT 0,
    banned_at INTEGER
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS stats (
    key TEXT PRIMARY KEY,
    value INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    owner_id INTEGER,
    items TEXT,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS saved_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL UNIQUE,
    url TEXT DEFAULT '',
    file_id TEXT DEFAULT '',
    is_video INTEGER DEFAULT 0,
    duration INTEGER DEFAULT 0,
    requester_id INTEGER DEFAULT 0,
    plays INTEGER DEFAULT 1,
    last_played INTEGER DEFAULT 0,
    created_at INTEGER DEFAULT 0
);
"""


class Database:
    def __init__(self, path: str = "musicbot.db"):
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ---------- helpers ----------
    async def _exec(self, sql: str, params: tuple = ()) -> None:
        await self._db.execute(sql, params)
        await self._db.commit()

    async def _one(self, sql: str, params: tuple = ()):
        cur = await self._db.execute(sql, params)
        return await cur.fetchone()

    async def _all(self, sql: str, params: tuple = ()):
        cur = await self._db.execute(sql, params)
        return await cur.fetchall()

    # ---------- users ----------
    async def add_user(self, user_id: int, username: str = "", first_name: str = "") -> None:
        await self._exec(
            "INSERT INTO users (user_id, username, first_name, joined_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
            "first_name=excluded.first_name",
            (user_id, username or "", first_name or "", int(time.time())),
        )

    async def get_user(self, user_id: int):
        return await self._one("SELECT * FROM users WHERE user_id=?", (user_id,))

    async def all_users(self):
        return await self._all("SELECT * FROM users ORDER BY joined_at DESC")

    async def count_users(self) -> int:
        row = await self._one("SELECT COUNT(*) AS c FROM users")
        return row["c"] if row else 0

    async def ban_user(self, user_id: int, banned_by: int = 0, reason: str = "") -> None:
        await self._exec(
            "INSERT INTO banned_users (user_id, banned_by, reason, banned_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO NOTHING",
            (user_id, banned_by, reason or "", int(time.time())),
        )
        await self._exec("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,))

    async def unban_user(self, user_id: int) -> None:
        await self._exec("DELETE FROM banned_users WHERE user_id=?", (user_id,))
        await self._exec("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))

    async def is_user_banned(self, user_id: int) -> bool:
        row = await self._one("SELECT 1 FROM banned_users WHERE user_id=?", (user_id,))
        return bool(row)

    # ---------- groups ----------
    async def add_group(self, chat_id: int, title: str = "", username: str = "") -> None:
        await self._exec(
            "INSERT INTO groups (chat_id, title, username, joined_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, username=excluded.username",
            (chat_id, title or "", username or "", int(time.time())),
        )

    async def get_group(self, chat_id: int):
        return await self._one("SELECT * FROM groups WHERE chat_id=?", (chat_id,))

    async def all_groups(self):
        return await self._all("SELECT * FROM groups ORDER BY joined_at DESC")

    async def count_groups(self) -> int:
        row = await self._one("SELECT COUNT(*) AS c FROM groups")
        return row["c"] if row else 0

    async def blacklist_group(self, chat_id: int, banned_by: int = 0) -> None:
        await self._exec(
            "INSERT INTO banned_groups (chat_id, banned_by, banned_at) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO NOTHING",
            (chat_id, banned_by, int(time.time())),
        )
        await self._exec("UPDATE groups SET is_blacklisted=1 WHERE chat_id=?", (chat_id,))

    async def whitelist_group(self, chat_id: int) -> None:
        await self._exec("DELETE FROM banned_groups WHERE chat_id=?", (chat_id,))
        await self._exec("UPDATE groups SET is_blacklisted=0 WHERE chat_id=?", (chat_id,))

    async def is_group_blacklisted(self, chat_id: int) -> bool:
        row = await self._one("SELECT 1 FROM banned_groups WHERE chat_id=?", (chat_id,))
        return bool(row)

    async def set_group_streaming(self, chat_id: int, enabled: bool) -> None:
        await self._exec(
            "UPDATE groups SET streaming_enabled=? WHERE chat_id=?", (1 if enabled else 0, chat_id)
        )

    async def is_group_streaming_enabled(self, chat_id: int) -> bool:
        row = await self._one("SELECT streaming_enabled FROM groups WHERE chat_id=?", (chat_id,))
        return bool(row and row["streaming_enabled"])

    # ---------- admins ----------
    async def add_admin(self, user_id: int, added_by: int = 0) -> None:
        await self._exec(
            "INSERT INTO admins (user_id, added_by, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO NOTHING",
            (user_id, added_by, int(time.time())),
        )

    async def remove_admin(self, user_id: int) -> None:
        await self._exec("DELETE FROM admins WHERE user_id=?", (user_id,))

    async def all_admins(self):
        return await self._all("SELECT * FROM admins ORDER BY added_at DESC")

    async def is_admin(self, user_id: int) -> bool:
        row = await self._one("SELECT 1 FROM admins WHERE user_id=?", (user_id,))
        return bool(row)

    # ---------- settings ----------
    async def set_setting(self, key: str, value: str) -> None:
        await self._exec(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    async def get_setting(self, key: str, default: str = "") -> str:
        row = await self._one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    # ---------- stats ----------
    async def bump_stat(self, key: str, by: int = 1) -> None:
        await self._exec(
            "INSERT INTO stats (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=value+excluded.value",
            (key, by),
        )

    async def get_stat(self, key: str) -> int:
        row = await self._one("SELECT value FROM stats WHERE key=?", (key,))
        return row["value"] if row else 0

    # ---------- playlists ----------
    async def save_playlist(self, name: str, owner_id: int, items: list) -> None:
        import json

        await self._exec(
            "INSERT INTO playlists (name, owner_id, items, created_at) VALUES (?, ?, ?, ?)",
            (name, owner_id, json.dumps(items), int(time.time())),
        )

    async def get_playlists(self, owner_id: int):
        return await self._all(
            "SELECT * FROM playlists WHERE owner_id=? ORDER BY created_at DESC", (owner_id,)
        )

    async def delete_playlist(self, playlist_id: int) -> None:
        await self._exec("DELETE FROM playlists WHERE id=?", (playlist_id,))

    # ---------- saved tracks (auto library of everything played) ----------
    async def save_track(
        self,
        *,
        title: str,
        source: str,
        url: str = "",
        file_id: str = "",
        is_video: bool = False,
        duration: int = 0,
        requester_id: int = 0,
    ) -> None:
        """Upsert a played track; bumps play count when re-played."""
        key = (url or file_id).strip()
        if not key:
            return
        now = int(time.time())
        await self._exec(
            "INSERT INTO saved_tracks (title, source, source_key, url, file_id, is_video,"
            " duration, requester_id, plays, last_played, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)"
            " ON CONFLICT(source_key) DO UPDATE SET"
            " title=excluded.title, source=excluded.source, is_video=excluded.is_video,"
            " duration=excluded.duration, plays=plays+1, last_played=excluded.last_played",
            (
                title, source, key, url, file_id,
                1 if is_video else 0, duration, requester_id, now, now,
            ),
        )

    async def saved_tracks_page(self, limit: int = 6, offset: int = 0):
        return await self._all(
            "SELECT * FROM saved_tracks ORDER BY last_played DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )

    async def count_saved_tracks(self) -> int:
        row = await self._one("SELECT COUNT(*) AS c FROM saved_tracks")
        return row["c"] if row else 0

    async def get_saved_track(self, track_id: int):
        return await self._one("SELECT * FROM saved_tracks WHERE id=?", (track_id,))
