"""
Tiny in-memory rate limiter (per user).
Prevents callback/command spam from breaking the UX or hammering Telegram.
"""
import time
from collections import defaultdict, deque

from pyrogram.types import CallbackQuery, Message

import config


class RateLimiter:
    def __init__(self, limit: int = 6, window: float = 4.0):
        self.limit = limit
        self.window = window
        self._hits: dict[int, deque] = defaultdict(deque)

    def hit(self, user_id: int) -> bool:
        """Record an action; return False if the user is rate-limited."""
        now = time.monotonic()
        dq = self._hits[user_id]
        while dq and now - dq[0] > self.window:
            dq.popleft()
        if len(dq) >= self.limit:
            return False
        dq.append(now)
        return True

    def allow(self, user_id: int) -> bool:
        return self.hit(user_id)


limiter = RateLimiter(config.RATE_LIMIT_CALLBACKS, config.RATE_LIMIT_WINDOW_S)


def rate_limited(func):
    """Decorator for pyrogram handlers — silently drops over-limit calls."""

    async def wrapper(client, obj, *args, **kwargs):
        uid = getattr(obj, "from_user", None)
        if uid is None:
            return
        if not limiter.allow(uid.id):
            return
        return await func(client, obj, *args, **kwargs)

    return wrapper


async def too_fast(obj) -> None:
    """Notify the user they are being rate limited."""
    try:
        if isinstance(obj, CallbackQuery):
            await obj.answer("🐢 Slow down!", show_alert=False)
        elif isinstance(obj, Message):
            await obj.reply("🐢 Slow down! Try again in a few seconds.")
    except Exception:
        pass
