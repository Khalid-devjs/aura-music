"""
Owner secret controls — soft shutdown + /kaboom revival.

How it works (smart design):
  • The bot NEVER fully exits from a command. A "shutdown" flips bot_offline=True:
    every handler gate checks it and refuses service to everyone EXCEPT the owner.
  • The owner can always bring it back with /kaboom (or any owner command) —
    a fully-dead process can't hear anything, so soft shutdown is the only
    design where "I shut it down, I bring it back" actually works.
  • The owner-only 🔒 secret button lives in the main menu; tapping it shows
    the secret command list.
  • "Aura knows me as its owner" — /start and any owner interaction greet by name.
"""
import logging
import os
import sys

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

import config
from buttons import inline as kb
from handlers import context as ctx
from handlers.requests import bot_offline, is_bot_offline
from modules import filters as guard
from modules.helpers import is_group, log_event, safe_edit

logger = logging.getLogger("auramusic")

SECRET_TEXT = (
    "🔒 **Owner Secret Panel**\n\n"
    "You are the **owner** — Aura knows you. 👑\n\n"
    "**Secret commands:**\n"
    "▸ `/shutdown` — soft-shut the bot (dead to everyone but **you**)\n"
    "▸ `/kaboom` — bring it back online instantly\n"
    "▸ `/owner` — show this panel again\n\n"
    "While soft-shutdown is active, everyone else gets *\"Aura is offline\"* —\n"
    "only you can wake it. 🔋"
)


def register(app: Client) -> None:
    # ------------------------------------------------------------------
    # Owner greeting — "Aura knows me as its owner"
    # ------------------------------------------------------------------
    @app.on_message(filters.command("owner", prefixes=["/", "!"]))
    async def owner_cmd(client: Client, message: Message):
        user = message.from_user
        if not user or not guard.is_owner(user.id):
            await message.reply("❌ This is a **secret owner command**. Nice try. 😏")
            return
        state = "🔴 **OFFLINE** (soft-shutdown active — everyone else is blocked)" if is_bot_offline() else "🟢 **ONLINE**"
        await message.reply(
            f"👑 **Welcome back, {user.first_name or 'Owner'}!**\n\n"
            f"Bot status: {state}\n\n"
            f"{SECRET_TEXT}",
            reply_markup=kb.owner_secret_kb(is_bot_offline()),
        )

    # ------------------------------------------------------------------
    # /shutdown — owner only, soft shutdown
    # ------------------------------------------------------------------
    @app.on_message(filters.command("shutdown", prefixes=["/", "!"]))
    async def shutdown_cmd(client: Client, message: Message):
        user = message.from_user
        if not user or not guard.is_owner(user.id):
            await message.reply("❌ Only the **owner** can shut me down. 😏")
            return
        if is_bot_offline():
            await message.reply("🔴 I'm **already offline** (soft-shutdown). Use `/kaboom` to bring me back!")
            return
        global bot_offline
        bot_offline = True
        await log_event(app, f"🛑 Owner {user.id} soft-shutdown the bot")
        await message.reply(
            "🛑 **Aura is going offline.**\n\n"
            "Everyone else will see *\"Aura is offline\"* — I'll only hear **you**.\n"
            "Bring me back anytime with `/kaboom`. 🔋"
        )

    # ------------------------------------------------------------------
    # /kaboom — owner only, bring it back
    # ------------------------------------------------------------------
    @app.on_message(filters.command("kaboom", prefixes=["/", "!"]))
    async def kaboom_cmd(client: Client, message: Message):
        user = message.from_user
        if not user or not guard.is_owner(user.id):
            await message.reply("❌ Nice try — but **kaboom** is for the owner only. 💥😏")
            return
        global bot_offline
        if not is_bot_offline():
            await message.reply("⚡ I'm **already online**, boss! Ready to rock. 🎧")
            return
        bot_offline = False
        await log_event(app, f"⚡ Owner {user.id} revived the bot with /kaboom")
        await message.reply(
            "💥 **KABOOM!** I'm back online! 🎉\n"
            "Music, videos, everything — fully alive again. Let's go! 🎧✨"
        )

    # ------------------------------------------------------------------
    # 🔒 Secret owner button in main menu
    # ------------------------------------------------------------------
    @app.on_callback_query(filters.regex(r"^owsec:"))
    async def owsec_cb(client: Client, cb: CallbackQuery):
        user = cb.from_user
        if not user or not guard.is_owner(user.id):
            await cb.answer("🔒 Secret owner area — not for you! 😏", show_alert=True)
            return
        action = cb.data.split(":", 1)[1]
        global bot_offline
        if action == "panel":
            await cb.answer()
            state = "🔴 OFFLINE" if is_bot_offline() else "🟢 ONLINE"
            await safe_edit(
                cb.message,
                f"👑 **Owner Panel** — status: {state}\n\n{SECRET_TEXT}",
                kb.owner_secret_kb(is_bot_offline()),
            )
        elif action == "shutdown":
            if is_bot_offline():
                await cb.answer("Already offline!", show_alert=True)
                return
            bot_offline = True
            await log_event(app, f"🛑 Owner {user.id} soft-shutdown via secret button")
            await cb.answer("🛑 Going offline…", show_alert=False)
            await safe_edit(cb.message, "🛑 **Aura is offline.**\n\nOnly you can wake me — `/kaboom`. 🔋", kb.owner_secret_kb(True))
        elif action == "kaboom":
            bot_offline = False
            await log_event(app, f"⚡ Owner {user.id} revived via secret button")
            await cb.answer("💥 KABOOM! Back online!", show_alert=False)
            await safe_edit(cb.message, "💥 **KABOOM!** I'm back online! 🎉", kb.owner_secret_kb(False))
        elif action == "close":
            try:
                await cb.message.delete()
            except Exception:
                pass
            await cb.answer("Closed", show_alert=False)