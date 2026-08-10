"""
Song requests — members request a track, group admins (or bot admins/owner) approve.
Also hosts the owner secret shutdown state (soft shutdown: bot ignores everyone
except the owner so /kaboom can always bring it back).
"""
import asyncio
import logging

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

import config
from buttons import inline as kb
from handlers import context as ctx
from modules import filters as guard
from modules.helpers import is_group, safe_edit
from modules.ratelimit import rate_limited
from player import downloader
from player.manager import manager
from player.stream import Streamer
from utils.formatters import format_duration, truncate

logger = logging.getLogger("auramusic")

# soft-shutdown state: when True the bot ignores everyone EXCEPT the owner.
# (A full os._exit shutdown could never be revived by /kaboom — this is the
# smart design: dead to the world, alive to its owner.)
bot_offline = False


def is_bot_offline() -> bool:
    return bot_offline


def register(app: Client) -> None:
    # ------------------------------------------------------------------
    # /request <song> — any member proposes a track; admins approve it.
    # ------------------------------------------------------------------
    @app.on_message(filters.command(["request", "req"], prefixes=["/", "!"]))
    async def request_cmd(client: Client, message: Message):
        user = message.from_user
        if not user or not await guard.can_use_bot(ctx.DB, user.id):
            return
        if not is_group(str(message.chat.type)):
            await message.reply("🎧 Use `/request` in a **group**!" )
            return
        chat_id = message.chat.id
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply(
                "🎵 **Request a song**\n\n"
                "Usage: `/request <song name or link>`\n"
                "Example: `/request Burna Boy - Last Last`\n\n"
                "A group admin will **approve** it and it plays. 🎧"
            )
            return
        # If the requester is already a controller (admin), play directly —
        # no approval needed.
        from handlers.player import _is_controller
        if await _is_controller(message, alert=False):
            await message.reply("👑 You're an admin — playing directly…")
            # reuse the play command path
            try:
                status = await message.reply("⏳ **Processing your request…**")
                track = await downloader.resolve_track(
                    client, message, parts[1], is_video=parts[1].lower().startswith("http")
                )
                from handlers.player import _enqueue  # local import avoids cycles
                await _enqueue(chat_id, track, client, status, message)
                await ctx.DB.bump_stat("video_plays" if track.is_video else "total_plays")
            except Exception as e:
                await message.reply(f"❌ **Could not load media.**\n\n`{truncate(str(e), 150)}`")
            return

        # normal member → create a pending request
        req_id = await ctx.DB.add_request(
            chat_id,
            user.id,
            user.first_name or user.username or "Member",
            parts[1],
            is_video=False,
        )
        await ctx.DB.add_user(user.id, user.username or "", user.first_name or "")
        # notify admins in the group
        await message.reply(
            f"📨 **Request sent to admins!**\n\n"
            f"🎵 `{truncate(parts[1], 80)}`\n"
            f"👤 {user.first_name or 'Member'}\n\n"
            f"_Waiting for an admin to approve…_",
            reply_markup=kb.request_status_kb(req_id),
        )
        # ping admins (best-effort)
        try:
            await _notify_admins_of_request(chat_id, req_id, parts[1], user)
        except Exception as e:
            logger.warning("request notify failed: %s", e)

    # ------------------------------------------------------------------
    # /requests — list pending requests (admins only)
    # ------------------------------------------------------------------
    @app.on_message(filters.command(["requests", "reqs"], prefixes=["/", "!"]))
    async def requests_cmd(client: Client, message: Message):
        user = message.from_user
        if not user or not await guard.can_use_bot(ctx.DB, user.id):
            return
        from handlers.player import _is_controller
        if not await _is_controller(message, alert=False):
            await message.reply("👑 **Admins only!** Use `/request <song>` to suggest a track.")
            return
        if not is_group(str(message.chat.type)):
            await message.reply("🎧 Use `/requests` in a **group**!")
            return
        pending = list(await ctx.DB.pending_requests(message.chat.id))
        if not pending:
            await message.reply("📭 **No pending requests.** All clear! ✨")
            return
        lines = []
        for i, r in enumerate(pending, start=1):
            icon = "🎬" if r["is_video"] else "🎵"
            lines.append(f"`{i}.` {icon} {truncate(r['query'], 60)} — 👤 {r['requester_name']}")
        txt = (
            f"📨 **Pending Requests ({len(pending)})**\n\n"
            + "\n".join(lines)
            + "\n\nTap **Approve** to play a request."
        )
        await message.reply(txt, reply_markup=kb.request_list_kb(pending))

    # ------------------------------------------------------------------
    # Request callbacks (approve / reject / list / status)
    # ------------------------------------------------------------------
    @app.on_callback_query(filters.regex(r"^req:"))
    @rate_limited
    async def req_cb(client: Client, cb: CallbackQuery):
        user = cb.from_user
        if not user or not await guard.can_use_bot(ctx.DB, user.id):
            await cb.answer("🚫 Banned.", show_alert=True)
            return
        parts = cb.data.split(":")
        action = parts[1]
        chat_id = cb.message.chat.id

        if action == "status":
            await cb.answer("Your request is with the admins 📨")
            return

        if action == "list":
            from handlers.player import _is_controller
            if not await _is_controller(cb, alert=False) and not guard.is_owner(user.id):
                await cb.answer("👑 Admins only!", show_alert=True)
                return
            pending = list(await ctx.DB.pending_requests(chat_id))
            if not pending:
                await safe_edit(cb.message, "📭 **No pending requests.** All clear! ✨", kb.back_to_main())
                await cb.answer()
                return
            lines = [f"`{i}.` {'🎬' if r['is_video'] else '🎵'} {truncate(r['query'], 60)} — 👤 {r['requester_name']}" for i, r in enumerate(pending, 1)]
            await safe_edit(cb.message, f"📨 **Pending Requests ({len(pending)})**\n\n" + "\n".join(lines), kb.request_list_kb(pending))
            await cb.answer()
            return

        if action in ("approve", "reject"):
            req_id = int(parts[2])
            from handlers.player import _is_controller
            # ONLY group admins / bot admins / owner can approve or reject
            ok = guard.is_owner(user.id) or await _is_controller(cb, alert=False)
            if not ok:
                await cb.answer("👑 Admins only!", show_alert=True)
                return
            row = await ctx.DB.get_request(req_id)
            if not row:
                await cb.answer("Request already handled.", show_alert=True)
                return
            if row["status"] != "pending":
                await cb.answer("Already handled.", show_alert=True)
                return

            if action == "reject":
                await ctx.DB.set_request_status(req_id, "rejected")
                await cb.answer("❌ Rejected.")
                try:
                    await client.send_message(
                        row["requester_id"],
                        f"❌ Your request `{truncate(row['query'], 60)}` was **rejected** by an admin.",
                    )
                except Exception:
                    pass
                await safe_edit(cb.message, f"❌ Rejected request from {row['requester_name']}: `{truncate(row['query'], 60)}`", kb.request_approve_kb(0))
                return

            # approve → mark + play
            await ctx.DB.set_request_status(req_id, "approved")
            await cb.answer("✅ Approved — playing!")
            try:
                await client.send_message(
                    row["requester_id"],
                    f"✅ Your request `{truncate(row['query'], 60)}` was **approved**! It's playing now. 🎧",
                )
            except Exception:
                pass
            await safe_edit(cb.message, f"✅ **Playing request** from {row['requester_name']}: `{truncate(row['query'], 60)}`", kb.request_approve_kb(0))
            # actually play it
            try:
                status = await client.send_message(chat_id, "⏳ **Processing approved request…**")
                track = await downloader.resolve_track(
                    client,
                    cb.message,
                    row["query"],
                    is_video=bool(row["is_video"]),
                )
                from handlers.player import _enqueue
                await _enqueue(chat_id, track, client, status, cb.message)
                await ctx.DB.bump_stat("video_plays" if track.is_video else "total_plays")
            except Exception as e:
                logger.warning("approved request play failed: %s", e)
                try:
                    await client.send_message(chat_id, f"❌ Could not play request: `{truncate(str(e), 120)}`")
                except Exception:
                    pass
            return


async def _notify_admins_of_request(chat_id: int, req_id: int, query: str, requester) -> None:
    """Best-effort: DM group admins about the new request."""
    try:
        from handlers.player import _is_controller
        # We can't easily enumerate chat admins without extra calls; instead we
        # post a small notice in the group — admins will see it there.
        await ctx.BOT_APP.send_message(
            chat_id,
            f"👆 A **new song request** is pending — tap **Approve** to play it.",
            reply_markup=kb.request_approve_kb(req_id),
        )
    except Exception:
        pass