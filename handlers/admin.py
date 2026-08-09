"""Admin panel: admins, bans, broadcast, groups, restart/shutdown, confirmations."""
import asyncio
import os
import subprocess
import sys

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

import config
from buttons import inline as kb
from handlers import context as ctx
from modules import filters as guard
from modules.helpers import ensure_int, is_group, log_event, safe_edit
from modules.ratelimit import rate_limited
from utils.formatters import truncate

ADMIN_TXT = "👑 **Admin Panel** — manage everything from here."


def register(app: Client) -> None:
    # ------------------------------------------------------------------
    # Admin menu callbacks
    # ------------------------------------------------------------------
    @app.on_callback_query(filters.regex(r"^adm:"))
    @rate_limited
    async def admin_cb(client: Client, cb: CallbackQuery):
        user = cb.from_user
        # SOFT-SHUTDOWN GATE — owner keeps full access
        from handlers.requests import is_bot_offline
        if user and is_bot_offline() and not guard.is_owner(user.id):
            await cb.answer("🛑 Aura is offline. 😴", show_alert=True)
            return
        if not await guard.is_admin(ctx.DB, user.id):
            await cb.answer("👑 Admins only!", show_alert=True)
            return
        action = cb.data.split(":", 1)[1]

        if action == "back":
            await cb.answer()
            await safe_edit(cb.message, ADMIN_TXT, kb.admin_menu())
            return
        if action == "groups":
            await cb.answer()
            await safe_edit(cb.message, "👥 **Group Management**", kb.group_menu())
            return
        if action == "owner":
            await cb.answer()
            if not guard.is_owner(user.id):
                await cb.answer("🔒 Owner only!", show_alert=True)
                return
            from handlers.owner import owner_text
            await safe_edit(cb.message, owner_text(), kb.owner_menu())
            return

        if action == "add":
            ctx.pending.set(user.id, "add_admin", chat_id=cb.message.chat.id)
            await cb.answer()
            await safe_edit(
                cb.message,
                "➕ **Add Admin**\n\nSend the user's **ID** or **username** (e.g. `123456789` or `@username`).",
                kb.back_to_main(),
            )
            return
        if action == "rm":
            ctx.pending.set(user.id, "rm_admin", chat_id=cb.message.chat.id)
            await cb.answer()
            await safe_edit(
                cb.message,
                "➖ **Remove Admin**\n\nSend the admin's **ID** or **username**.",
                kb.back_to_main(),
            )
            return
        if action == "list":
            await _admin_list(cb)
            return
        if action == "ban":
            ctx.pending.set(user.id, "ban_user", chat_id=cb.message.chat.id)
            await cb.answer()
            await safe_edit(
                cb.message, "🚫 **Ban User**\n\nSend the user's **ID** or **username** (reply to a message works too)."
            )
            return
        if action == "unban":
            ctx.pending.set(user.id, "unban_user", chat_id=cb.message.chat.id)
            await cb.answer()
            await safe_edit(cb.message, "✅ **Unban User**\n\nSend the user's **ID** or **username**.")
            return
        if action == "bcast":
            await cb.answer()
            await safe_edit(cb.message, "📢 **Broadcast**\n\nSend to which audience?", kb.broadcast_kb())
            return
        if action == "stats":
            await cb.answer()
            await _admin_stats(cb)
            return
        if action == "restart":
            await cb.answer()
            await safe_edit(
                cb.message,
                "🔄 **Restart bot?**",
                kb.confirm_kb("restart", "0"),
            )
            return
        if action == "shutdown":
            await cb.answer()
            await safe_edit(
                cb.message,
                "🛑 **Shutdown bot?**",
                kb.confirm_kb("shutdown", "0"),
            )
            return

    # ------------------------------------------------------------------
    # Broadcast audience choice
    # ------------------------------------------------------------------
    @app.on_callback_query(filters.regex(r"^bcast:"))
    @rate_limited
    async def bcast_cb(client: Client, cb: CallbackQuery):
        user = cb.from_user
        if not await guard.is_admin(ctx.DB, user.id):
            await cb.answer("👑 Admins only!", show_alert=True)
            return
        audience = cb.data.split(":", 1)[1]
        ctx.pending.set(user.id, "broadcast", audience=audience, chat_id=cb.message.chat.id)
        await cb.answer()
        await safe_edit(cb.message, f"📢 **Broadcast to {audience}**\n\nNow send the message you want to broadcast (text / media).")

    # ------------------------------------------------------------------
    # Confirmations: restart / shutdown / leave / blacklist
    # ------------------------------------------------------------------
    @app.on_callback_query(filters.regex(r"^cf:"))
    @rate_limited
    async def confirm_cb(client: Client, cb: CallbackQuery):
        user = cb.from_user
        if not await guard.is_admin(ctx.DB, user.id):
            await cb.answer("👑 Admins only!", show_alert=True)
            return
        _, answer, action, target = cb.data.split(":", 3)
        if answer == "no":
            await cb.answer("Cancelled ✅")
            try:
                await cb.message.delete()
            except Exception:
                pass
            return

        if action == "restart":
            await cb.answer("🔄 Restarting…")
            await cb.message.edit_text("🔄 **Restarting bot…**\n\nGive me a few seconds. 🚀")
            await log_event(app, f"🔄 Bot restarted by {user.first_name} ({user.id})")
            await _restart()
        elif action == "shutdown":
            await cb.answer("🛑 Shutting down…")
            await cb.message.edit_text("🛑 **Bot is going offline.**\n\nGoodbye! 👋")
            await log_event(app, f"🛑 Bot shutdown by {user.first_name} ({user.id})")
            await asyncio.sleep(1.5)
            os._exit(0)
        elif action == "leave":
            chat_id = int(target)
            await cb.answer("🚪 Leaving…")
            try:
                await ctx.STREAMER.leave(chat_id)
            except Exception:
                pass
            try:
                await app.leave_chat(chat_id)
            except Exception as e:
                await cb.message.edit_text(f"❌ Could not leave: `{truncate(str(e), 80)}`")
            await log_event(app, f"🚪 Bot left group {chat_id} (forced by {user.first_name})")
        elif action == "blacklist":
            chat_id = int(target)
            await ctx.DB.blacklist_group(chat_id, user.id)
            await cb.answer("🚫 Blacklisted")
            await cb.message.edit_text(f"🚫 Group `{chat_id}` **blacklisted**.")
            try:
                await ctx.STREAMER.leave(chat_id)
            except Exception:
                pass
            try:
                await app.leave_chat(chat_id)
            except Exception:
                pass
            await log_event(app, f"🚫 Group {chat_id} blacklisted by {user.first_name}")
        elif action == "shutdown_owner":
            await cb.answer("🛑 Shutting down…")
            await cb.message.edit_text("🛑 **Bot is going offline.** Goodbye! 👋")
            await asyncio.sleep(1.5)
            os._exit(0)

    # ------------------------------------------------------------------
    # Group management callbacks
    # ------------------------------------------------------------------
    @app.on_callback_query(filters.regex(r"^grp:"))
    @rate_limited
    async def group_cb(client: Client, cb: CallbackQuery):
        user = cb.from_user
        if not await guard.is_admin(ctx.DB, user.id):
            await cb.answer("👑 Admins only!", show_alert=True)
            return
        action = cb.data.split(":", 1)[1]
        in_group = is_group(str(cb.message.chat.type))

        if action == "back":
            await cb.answer()
            await safe_edit(cb.message, ADMIN_TXT, kb.admin_menu())
            return
        if action == "leave":
            await cb.answer()
            if in_group:
                chat_id = cb.message.chat.id
                await safe_edit(
                    cb.message,
                    f"🚪 **Leave `{chat_id}`?**",
                    kb.confirm_kb("leave", str(chat_id)),
                )
            else:
                ctx.pending.set(user.id, "leave_group", chat_id=0)
                await safe_edit(cb.message, "🚪 **Leave Group**\n\nSend the group's chat **ID**.")
            return
        if action == "bl":
            await cb.answer()
            if in_group:
                chat_id = cb.message.chat.id
                await safe_edit(
                    cb.message,
                    f"🚫 **Blacklist group `{chat_id}`?**\n\nBot will leave and refuse to join again.",
                    kb.confirm_kb("blacklist", str(chat_id)),
                )
            else:
                ctx.pending.set(user.id, "blacklist_group", chat_id=0)
                await safe_edit(cb.message, "🚫 **Blacklist Group**\n\nSend the group's chat **ID**.")
            return
        if action == "wl":
            await cb.answer()
            ctx.pending.set(user.id, "whitelist_group", chat_id=0)
            await safe_edit(cb.message, "✅ **Whitelist Group**\n\nSend the group's chat **ID**.")
            return
        if action == "list":
            await _group_list(cb)
            return
        if action == "set":
            await cb.answer()
            if in_group:
                g = await ctx.DB.get_group(cb.message.chat.id)
                streaming = bool(g and g["streaming_enabled"])
                await safe_edit(
                    cb.message,
                    f"⚙️ **Settings for group `{cb.message.chat.id}`**",
                    kb.group_setting_kb(cb.message.chat.id, streaming),
                )
            else:
                ctx.pending.set(user.id, "group_settings", chat_id=0)
                await safe_edit(cb.message, "⚙️ **Group Settings**\n\nSend the group's chat **ID**.")
            return
        if action.startswith("set:"):
            parts = action.split(":")
            chat_id = int(parts[1])
            g = await ctx.DB.get_group(chat_id)
            streaming = not bool(g and g["streaming_enabled"])
            await ctx.DB.set_group_streaming(chat_id, streaming)
            await cb.answer("⚙️ Updated")
            await safe_edit(
                cb.message,
                f"⚙️ **Settings for group `{chat_id}`**",
                kb.group_setting_kb(chat_id, streaming),
            )
            return

    # ------------------------------------------------------------------
    # Pending text processing for admin flows
    # ------------------------------------------------------------------
    @app.on_message(filters.private, group=3)
    async def admin_input(client: Client, message: Message):
        user = message.from_user
        if not user or not message.text:
            return
        req = ctx.pending.pop(user.id)
        if not req:
            return
        action = req["action"]
        text = message.text.strip()
        target = await _resolve_target(app, message, text)

        if action == "add_admin":
            if target is None:
                await message.reply("❌ User not found. Send a valid **ID** or **username**.")
                return
            if guard.is_owner(target):
                await message.reply("👑 The owner is always an admin.")
                return
            await ctx.DB.add_admin(target, user.id)
            await message.reply(f"✅ **{target}** is now an admin.")
            await log_event(app, f"➕ {target} promoted to admin by {user.first_name}")
        elif action == "rm_admin":
            if target is None:
                await message.reply("❌ User not found.")
                return
            if guard.is_owner(target):
                await message.reply("👑 Cannot remove the owner.")
                return
            await ctx.DB.remove_admin(target)
            await message.reply(f"➖ **{target}** removed from admins.")
            await log_event(app, f"➖ {target} demoted by {user.first_name}")
        elif action == "ban_user":
            if target is None:
                await message.reply("❌ User not found.")
                return
            await ctx.DB.ban_user(target, user.id)
            await message.reply(f"🚫 **{target}** banned from the bot.")
            await log_event(app, f"🚫 {target} banned by {user.first_name}")
        elif action == "unban_user":
            if target is None:
                await message.reply("❌ User not found.")
                return
            await ctx.DB.unban_user(target)
            await message.reply(f"✅ **{target}** unbanned.")
        elif action == "leave_group":
            await _leave_group(app, message, target)
        elif action == "blacklist_group":
            await _blacklist_group(app, message, target)
        elif action == "whitelist_group":
            await _whitelist_group(message, target)
        elif action == "group_settings":
            await _group_settings(app, message, target)
        elif action == "broadcast":
            await _do_broadcast(app, message, req["data"].get("audience", "users"))

    # ------------------------------------------------------------------
    # Group events: bot added / auto-leave blacklisted
    # ------------------------------------------------------------------
    @app.on_chat_member_updated()
    async def on_member_update(client: Client, update):
        try:
            if update.new_chat_member and update.new_chat_member.user and update.new_chat_member.user.id == (await app.get_me()).id:
                chat = update.chat
                if is_group(str(chat.type)):
                    if await ctx.DB.is_group_blacklisted(chat.id):
                        await client.leave_chat(chat.id)
                        return
                    await ctx.DB.add_group(chat.id, chat.title or "", getattr(chat, "username", "") or "")
                    await log_event(app, f"➕ Added to group {chat.title} ({chat.id})")
        except Exception:
            pass


# ----------------------------------------------------------------------
async def _resolve_target(app: Client, message: Message, text: str):
    """Turn an id / @username / replied message into a user_id."""
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    if text.startswith("@"):
        try:
            u = await app.get_users(text)
            return u.id
        except Exception:
            return None
    try:
        return int(text)
    except ValueError:
        try:
            u = await app.get_users(text)
            return u.id
        except Exception:
            return None


async def _admin_list(cb: CallbackQuery):
    admins = await ctx.DB.all_admins()
    lines = [f"👑 **Owner:** `{config.OWNER_ID}`"]
    if not admins:
        lines.append("\n_(no extra admins)_")
    for a in admins:
        lines.append(f"👤 **{a['user_id']}** (added by {a['added_by']})")
    await safe_edit(cb.message, "👥 **Admin List**\n\n" + "\n".join(lines), kb.admin_menu())


async def _admin_stats(cb: CallbackQuery):
    from handlers.start import _send_stats

    await _send_stats(cb)


async def _group_list(cb: CallbackQuery):
    groups = await ctx.DB.all_groups()
    if not groups:
        await safe_edit(cb.message, "📋 **No groups yet.** Add the bot to a group!", kb.group_menu())
        return
    lines = [f"📋 **Groups: {len(groups)}**\n"]
    for g in groups[:20]:
        flag = "🚫" if g["is_blacklisted"] else "✅"
        title = truncate(g["title"] or str(g["chat_id"]), 30)
        lines.append(f"{flag} `{g['chat_id']}` — {title}")
    if len(groups) > 20:
        lines.append(f"\n_…and {len(groups) - 20} more_")
    await safe_edit(cb.message, "\n".join(lines), kb.group_menu())


async def _leave_group(app: Client, message: Message, target):
    if target is None:
        await message.reply("❌ Send a valid group **chat ID**.")
        return
    await ctx.STREAMER.leave(target)
    try:
        await app.leave_chat(target)
        await message.reply(f"🚪 Left group `{target}`.")
    except Exception as e:
        await message.reply(f"❌ Could not leave: `{truncate(str(e), 80)}`")


async def _blacklist_group(app: Client, message: Message, target):
    if target is None:
        await message.reply("❌ Send a valid group **chat ID**.")
        return
    await ctx.DB.blacklist_group(target, message.from_user.id)
    try:
        await ctx.STREAMER.leave(target)
    except Exception:
        pass
    try:
        await app.leave_chat(target)
    except Exception:
        pass
    await message.reply(f"🚫 Group `{target}` **blacklisted** and left.")


async def _whitelist_group(message: Message, target):
    if target is None:
        await message.reply("❌ Send a valid group **chat ID**.")
        return
    await ctx.DB.whitelist_group(target)
    await message.reply(f"✅ Group `{target}` **whitelisted**.")


async def _group_settings(app: Client, message: Message, target):
    if target is None:
        await message.reply("❌ Send a valid group **chat ID**.")
        return
    g = await ctx.DB.get_group(target)
    streaming = bool(g and g["streaming_enabled"])
    await message.reply(
        f"⚙️ **Settings for group `{target}`**",
        reply_markup=kb.group_setting_kb(target, streaming),
    )


async def _do_broadcast(app: Client, message: Message, audience: str):
    await message.reply("📢 **Broadcasting…** (this may take a while)")
    sent = failed = 0
    if audience in ("users", "all"):
        for row in await ctx.DB.all_users():
            try:
                await app.send_message(row["user_id"], message.text or "📢")
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
    if audience in ("groups", "all"):
        for row in await ctx.DB.all_groups():
            try:
                await app.send_message(row["chat_id"], message.text or "📢")
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
    await message.reply(f"📢 **Broadcast done** — ✅ {sent} delivered, ❌ {failed} failed.")
    await ctx.DB.bump_stat("broadcasts")
    await log_event(app, f"📢 Broadcast by {message.from_user.first_name}: {sent} ok / {failed} failed")


def _restart() -> None:
    """Restart the bot in place (works under systemd / docker / nohup)."""
    os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
    try:
        subprocess.Popen([sys.executable, "main.py"])
    except Exception:
        pass
    os._exit(0)
