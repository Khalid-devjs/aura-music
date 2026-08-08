"""Shared helpers for handlers."""
from pyrogram.types import InlineKeyboardMarkup, Message

import config


async def safe_edit(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    disable_web_page_preview: bool = True,
) -> Message:
    """Edit a message; fall back to delete + send (e.g. after auto-delete)."""
    try:
        return await message.edit_text(
            text, reply_markup=reply_markup, disable_web_page_preview=disable_web_page_preview
        )
    except Exception:
        try:
            await message.delete()
        except Exception:
            pass
        return await message.reply(text, reply_markup=reply_markup)


async def send_or_edit(message: Message, text: str, reply_markup=None) -> Message:
    try:
        return await safe_edit(message, text, reply_markup)
    except Exception:
        return await message.reply(text, reply_markup=reply_markup)


def is_group(chat_type: str) -> bool:
    return chat_type in ("group", "supergroup")


def ensure_int(value) -> int:
    return int(str(value).strip().lstrip("+-") or 0)


async def log_event(app, text: str) -> None:
    """Send an event to the logs channel (if configured)."""
    if config.LOG_CHANNEL_ID:
        try:
            await app.send_message(config.LOG_CHANNEL_ID, text)
        except Exception:
            pass
