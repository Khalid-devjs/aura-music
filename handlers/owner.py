"""Owner-only dashboard: users, groups, system info, update, logs."""
import os
import subprocess
import sys
import time

import psutil
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from buttons import inline as kb
from handlers import context as ctx
from modules import filters as guard
from modules.helpers import ensure_int, log_event, safe_edit
from modules.ratelimit import rate_limited
from utils.formatters import truncate


def owner_text() -> str:
    return "👑 **Owner Dashboard**\n\nFull control over the bot."


def register(app: Client) -> None:
    @app.on_callback_query(filters.regex(r"^ow:"))
    @rate_limited
    async def owner_cb(client: Client, cb: CallbackQuery):
        user = cb.from_user
        if not guard.is_owner(user.id):
            await cb.answer("🔒 Owner only!", show_alert=True)
            return
        action = cb.data.split(":", 1)[1]

        if action == "back":
            await cb.answer()
            await safe_edit(cb.message, "👑 **Admin Panel**", kb.admin_menu())
            return
        if action == "users":
            await cb.answer()
            await _list_users(cb)
            return
        if action == "groups":
            await cb.answer()
            await _list_groups(cb)
            return
        if action == "active":
            await cb.answer()
            await _active_vcs(cb)
            return
        if action == "sys":
            await cb.answer()
            await safe_edit(cb.message, _system_info(), kb.owner_menu())
            return
        if action == "ban":
            ctx.pending.set(user.id, "owner_ban_user", chat_id=0)
            await cb.answer()
            await safe_edit(cb.message, "🚫 **Ban User**\n\nSend the user's **ID** or **username**.")
            return
        if action == "unban":
            ctx.pending.set(user.id, "owner_unban_user", chat_id=0)
            await cb.answer()
            await safe_edit(cb.message, "✅ **Unban User**\n\nSend the user's **ID** or **username**.")
            return
        if action == "ban_group":
            ctx.pending.set(user.id, "owner_ban_group", chat_id=0)
            await cb.answer()
            await safe_edit(cb.message, "🚫 **Ban Group**\n\nSend the group's chat **ID**.")
            return
        if action == "unban_group":
            ctx.pending.set(user.id, "owner_unban_group", chat_id=0)
            await cb.answer()
            await safe_edit(cb.message, "✅ **Unban Group**\n\nSend the group's chat **ID**.")
            return
        if action == "admins":
            await cb.answer()
            admins = await ctx.DB.all_admins()
            lines = [f"👑 **Owner:** `{config_owner()}`"]
            lines += [f"👤 {a['user_id']}" for a in admins] or ["_(no extra admins)_"]
            await safe_edit(cb.message, "👥 **Admins**\n\n" + "\n".join(lines), kb.owner_menu())
            return
        if action == "logs":
            await cb.answer()
            await _send_logs(cb)
            return
        if action == "update":
            await cb.answer()
            await cb.message.edit_text("🔄 **Updating…**")
            await log_event(app, f"🔄 Update triggered by owner {user.id}")
            try:
                r = subprocess.run(
                    ["git", "pull"], capture_output=True, text=True, timeout=60
                )
                out = (r.stdout or "")[-800:] + (r.stderr or "")[-200:]
            except Exception as e:
                out = str(e)
            await cb.message.edit_text(f"🔄 **Git pull result:**\n\n`{out or 'ok'}`")
            await _restart_after_update(cb.message)
            return
        if action == "shutdown":
            await safe_edit(
                cb.message,
                "🛑 **Shutdown the bot?**",
                kb.confirm_kb("shutdown_owner", "0"),
            )
            return

    # ---- ctx.pending owner inputs (private) ----
    @app.on_message(filters.private, group=4)
    async def owner_input(client: Client, message: Message):
        user = message.from_user
        if not guard.is_owner(user.id) or not message.text:
            return
        req = ctx.pending.pop(user.id)
        if not req:
            return
        action = req["action"]
        text = message.text.strip()

        if action in ("owner_ban_user", "owner_unban_user"):
            try:
                target = int(text)
            except ValueError:
                await message.reply("❌ Send a numeric user **ID**.")
                return
            if action == "owner_ban_user":
                await ctx.DB.ban_user(target, user.id)
                await message.reply(f"🚫 User `{target}` banned.")
                await log_event(app, f"🚫 {target} banned by owner")
            else:
                await ctx.DB.unban_user(target)
                await message.reply(f"✅ User `{target}` unbanned.")
            return

        if action in ("owner_ban_group", "owner_unban_group"):
            try:
                target = int(text)
            except ValueError:
                await message.reply("❌ Send a numeric group **ID**.")
                return
            if action == "owner_ban_group":
                await ctx.DB.blacklist_group(target, user.id)
                try:
                    await app.leave_chat(target)
                except Exception:
                    pass
                await message.reply(f"🚫 Group `{target}` banned.")
            else:
                await ctx.DB.whitelist_group(target)
                await message.reply(f"✅ Group `{target}` unbanned.")
            return


def config_owner():
    import config

    return config.OWNER_ID


async def _list_users(cb: CallbackQuery):
    rows = await ctx.DB.all_users()
    if not rows:
        await safe_edit(cb.message, "👥 **No users yet.**", kb.owner_menu())
        return
    lines = [f"👥 **Users: {len(rows)}**\n"]
    for u in rows[:25]:
        flag = "🚫" if u["is_banned"] else "🟢"
        name = truncate(u["first_name"] or u["username"] or str(u["user_id"]), 25)
        lines.append(f"{flag} `{u['user_id']}` — {name}")
    if len(rows) > 25:
        lines.append(f"\n_…and {len(rows) - 25} more_")
    await safe_edit(cb.message, "\n".join(lines), kb.owner_menu())


async def _list_groups(cb: CallbackQuery):
    rows = await ctx.DB.all_groups()
    if not rows:
        await safe_edit(cb.message, "👥 **No groups yet.**", kb.owner_menu())
        return
    lines = [f"👥 **Groups: {len(rows)}**\n"]
    for g in rows[:25]:
        flag = "🚫" if g["is_blacklisted"] else "🟢"
        lines.append(f"{flag} `{g['chat_id']}` — {truncate(g['title'] or '', 25)}")
    if len(rows) > 25:
        lines.append(f"\n_…and {len(rows) - 25} more_")
    await safe_edit(cb.message, "\n".join(lines), kb.owner_menu())


async def _active_vcs(cb: CallbackQuery):
    calls = await ctx.STREAMER.active_calls()
    if not calls:
        await safe_edit(cb.message, "🎧 **No active voice chats.**", kb.owner_menu())
        return
    lines = [f"🎧 **Active calls: {len(calls)}**\n"]
    for c in calls:
        lines.append(f"💬 `{c['chat_id']}` — {c['participants']} participants")
    await safe_edit(cb.message, "\n".join(lines), kb.owner_menu())


def _system_info() -> str:
    uptime = int(time.time() - ctx.START_TIME)
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(".")
    active = len(ctx.STREAMER.active_calls()) if ctx.STREAMER else 0
    return (
        "🖥️ **System Information**\n\n"
        f"⏱ **Bot uptime:** {h}h {m}m {s}s\n"
        f"🎧 **Active calls:** {active}\n"
        f"🧠 **CPU:** {cpu}%\n"
        f"💾 **RAM:** {mem.percent}% ({mem.used // (1024**2)}MB / {mem.total // (1024**2)}MB)\n"
        f"💿 **Disk:** {disk.percent}% used\n"
        f"🐍 **Python:** {sys.version.split()[0]}"
    )


async def _send_logs(cb: CallbackQuery):
    path = "logs/musicbot.log"
    try:
        if os.path.exists(path):
            await cb.message.reply_document(path, caption="📜 **Recent logs**")
        else:
            await safe_edit(cb.message, "📜 No log file yet.", kb.owner_menu())
    except Exception as e:
        await safe_edit(cb.message, f"❌ `{truncate(str(e), 80)}`", kb.owner_menu())


async def _restart_after_update(message: Message):
    try:
        await message.reply("🔄 **Restarting with new code…** 🚀")
        os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
        subprocess.Popen([sys.executable, "main.py"])
        os._exit(0)
    except Exception:
        pass
