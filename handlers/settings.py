"""Settings + group auto-registration / blacklist auto-leave."""
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

import config
from buttons import inline as kb
from handlers import context as ctx
from modules import filters as guard
from modules.helpers import is_group, log_event, safe_edit
from modules.ratelimit import rate_limited


def register(app: Client) -> None:
    @app.on_callback_query(filters.regex(r"^set:"))
    @rate_limited
    async def settings_cb(client: Client, cb: CallbackQuery):
        user = cb.from_user
        action = cb.data.split(":", 1)[1]
        if action == "autodel":
            current = (await ctx.DB.get_setting("auto_delete_ms", str(config.AUTO_DELETE_MS))) != "0"
            new = "0" if current else str(config.AUTO_DELETE_MS or 30000)
            await ctx.DB.set_setting("auto_delete_ms", new)
            await cb.answer(f"🗑️ Auto-delete {'ON' if new != '0' else 'OFF'}")
            await safe_edit(
                cb.message, "⚙️ **Settings**", kb.settings_menu(new != "0")
            )
            return

    # --------------------------------------------------------------
    # Group message traffic: register users/groups, auto-leave blacklists
    # --------------------------------------------------------------
    @app.on_message(filters.group, group=1)
    async def group_guard(client: Client, message: Message):
        try:
            chat = message.chat
            if is_group(str(chat.type)):
                if await ctx.DB.is_group_blacklisted(chat.id):
                    await client.leave_chat(chat.id)
                    return
                await ctx.DB.add_group(chat.id, chat.title or "", getattr(chat, "username", "") or "")
                if message.from_user:
                    await ctx.DB.add_user(
                        message.from_user.id,
                        message.from_user.username or "",
                        message.from_user.first_name or "",
                    )
        except Exception:
            pass
