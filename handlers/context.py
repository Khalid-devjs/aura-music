"""
Shared runtime context + pending multi-step input registry.
"""
import time

import config
from pyrogram import Client

from database.db import Database
from player.stream import Streamer

BOT_APP: Client = None           # bot client
USER_APP: Client = None          # user client (streaming)
DB: Database = None
STREAMER: Streamer = None
START_TIME: float = time.time()


def set_context(bot: Client, user: Client, db: Database, streamer: Streamer) -> None:
    global BOT_APP, USER_APP, DB, STREAMER
    BOT_APP, USER_APP, DB, STREAMER = bot, user, db, streamer


async def reload_streamer(session_string: str) -> None:
    global USER_APP, STREAMER
    old_user = USER_APP
    old_streamer = STREAMER
    if old_user and getattr(old_user, "is_connected", False):
        try:
            await old_user.stop()
        except Exception:
            pass
    USER_APP = Client(
        getattr(old_user, "name", "auramusic_user"),
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        session_string=session_string,
        workers=8,
    )
    STREAMER = Streamer(USER_APP)
    await USER_APP.start()
    await STREAMER.start()


class Pending:
    """Registry for multi-step flows: user_id -> {action, data, expires}."""

    TTL = 150  # seconds

    def __init__(self):
        self._p: dict[int, dict] = {}

    def set(self, user_id: int, action: str, **data) -> None:
        self._p[user_id] = {"action": action, "data": data, "expires": time.time() + self.TTL}

    def get(self, user_id: int) -> dict | None:
        item = self._p.get(user_id)
        if not item:
            return None
        if item["expires"] < time.time():
            self._p.pop(user_id, None)
            return None
        return item

    def pop(self, user_id: int) -> dict | None:
        item = self.get(user_id)
        if item:
            self._p.pop(user_id, None)
        return item


pending = Pending()
