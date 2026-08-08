"""Forward bot errors to the owner's DM.

Async bridge: a logging.Handler catches ERROR+ records from our loggers
("auramusic" + "pyrogram.dispatcher"), dedupes repeats, throttles sends
(1 per 15s), and delivers via the BOT client to the owner's Telegram DM.
"""
import asyncio
import logging
import time
import traceback

import config
from handlers import context as ctx

SEND_INTERVAL = 15.0   # min seconds between DM sends
DEDUPE_WINDOW = 60.0   # same error ignored again within this window
MAX_QUEUE = 50


class _DMBridge:
    """Async queue + throttled sender."""

    def __init__(self):
        self._queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self._last_sent = 0.0
        self._suppressed = 0
        self._task = None

    def push(self, text: str) -> None:
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            pass

    async def run(self) -> None:
        while True:
            text = await self._queue.get()
            now = time.monotonic()
            if now - self._last_sent < SEND_INTERVAL:
                self._suppressed += 1
                continue
            self._last_sent = now
            if self._suppressed:
                text = f"⚠️ _{self._suppressed} more error(s) suppressed._\n\n" + text
                self._suppressed = 0
            try:
                await ctx.BOT_APP.send_message(config.OWNER_ID, text)
            except Exception:
                pass  # never loop on send failures


_bridge = _DMBridge()


class DMLogHandler(logging.Handler):
    """Forwards ERROR+ records to the owner DM (deduped, throttled)."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self._recent = {}  # fingerprint -> last seen (monotonic)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name not in ("auramusic", "pyrogram.dispatcher"):
                return
            fp = (record.name, record.getMessage()[:100])
            now = time.monotonic()
            if now - self._recent.get(fp, 0) < DEDUPE_WINDOW:
                return
            if len(self._recent) > 200:
                self._recent.clear()
            self._recent[fp] = now

            msg = record.getMessage()[:400]
            exc = ""
            if record.exc_info:
                exc = "\n```\n" + "".join(traceback.format_exception(*record.exc_info))[-1200:] + "\n```"
            _bridge.push(f"🚨 **Aura Music error**\n\n**Source:** `{record.name}`\n**Error:** {msg}{exc}")
        except Exception:
            pass  # the reporter must never break the bot


def install() -> None:
    """Attach the DM handler + start the sender task. Call inside main() (after set_context)."""
    logging.getLogger().addHandler(DMLogHandler())
    _bridge._task = asyncio.get_event_loop().create_task(_bridge.run())
    logging.getLogger("auramusic").info("Error reporter installed → DM %s", config.OWNER_ID)
