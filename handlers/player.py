"""Music/video playback flow + player control buttons + queue."""
import asyncio
import logging

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, ChatPrivileges, Message

import config
from buttons import inline as kb
from handlers import context as ctx
from modules import filters as guard
from modules.helpers import is_group, log_event, safe_edit
from modules.ratelimit import rate_limited
from player import downloader
from player.manager import manager, Track
from utils.formatters import format_duration, truncate

logger = logging.getLogger("auramusic")

PROCESSING = "⏳ **Processing your request…**"

GROUP_ONLY_MSG = (
    "🎧 This command works in a **group** only!\n\n"
    "Add me to a group, open its **voice chat**, and use it there."
)


async def _group_only(message: Message) -> bool:
    """True when used inside a group/supergroup; otherwise replies and returns False."""
    if is_group(str(message.chat.type)):
        return True
    try:
        await message.reply(GROUP_ONLY_MSG)
    except Exception:
        pass
    return False


def register(app: Client) -> None:
    # ------------------------------------------------------------------
    # Incoming media / text (after tapping Play Music / Play Video)
    # ------------------------------------------------------------------
    @app.on_message(
        filters.private | filters.group,
        group=2,
    )
    async def on_user_input(client: Client, message: Message):
        user = message.from_user
        if not user:
            return
        # SOFT-SHUTDOWN GATE: bot is offline except for the owner
        from handlers.requests import is_bot_offline
        if is_bot_offline() and not guard.is_owner(user.id):
            await message.reply("🛑 **Aura is offline.** Please try again later. 😴")
            return
        if not await guard.can_use_bot(ctx.DB, user.id):
            return
        req = ctx.pending.pop(user.id)
        if not req:
            # no pending button request — BUT a direct Telegram audio/video file
            # sent to the bot still counts as an implicit play request
            has_media = bool(
                message.audio
                or message.video
                or (
                    message.document
                    and message.document.mime_type
                    and message.document.mime_type.startswith(("audio/", "video/"))
                )
            )
            if not (has_media and message.media):
                # plain text with no pending flow → just register the user
                await ctx.DB.add_user(user.id, user.username or "", user.first_name or "")
                return
            req = {
                "action": "play_video"
                if message.video is not None
                or (
                    message.document
                    and message.document.mime_type
                    and message.document.mime_type.startswith("video/")
                )
                else "play_music",
                "data": {"chat_id": message.chat.id},
            }
        action = req["action"]
        if action not in ("play_music", "play_video"):
            # not a play request — put it back for other handlers
            ctx.pending.set(user.id, action, **(req.get("data") or {}))
            return
        target_chat = req["data"].get("chat_id") or message.chat.id
        is_video = action == "play_video"

        # ONLY group admins / bot admins / owner may PLAY music or video.
        # Regular members use /request and wait for an admin to approve.
        if not await _is_controller(message, alert=False):
            await message.reply(
                "👑 **Admins only!**\n\n"
                "Only group admins (and the bot owner) can play music/videos.\n"
                "Members: send `/request <song name>` and an admin will approve it. 🎧"
            )
            return
        if not await _group_only(message):
            return

        # a Telegram media file?
        has_media = bool(
            message.audio or message.video
            or (
                message.document
                and message.document.mime_type
                and message.document.mime_type.startswith(("audio/", "video/"))
            )
        )
        if has_media and message.media:
            query = "file"
        elif message.text and message.text.strip():
            query = message.text.strip()
        else:
            return

        # check per-group streaming setting
        if is_group(str(message.chat.type)) and not await ctx.DB.is_group_streaming_enabled(target_chat):
            await message.reply("🚫 Streaming is **disabled** in this group.")
            return

        # commands/menu work in groups only — the streamer needs a voice chat to join
        if not is_group(str(message.chat.type)):
            await message.reply(
                "🎧 This works in a **group** only!\n\n"
                "Add me to a group, open its **voice chat**, then tap **🎵 Play Music** there.\n\n"
                "Or browse your **💾 Saved library** below:",
                reply_markup=kb.saved_hint_kb(),
            )
            return

        # boss bot auto-adds the streamer userbot if it's missing from the group
        if not await _ensure_streamer_in_chat(target_chat):
            await message.reply(
                "⚠️ The streamer **@teleaddbyking** isn't a member here.\n\n"
                "Telegram doesn't let bots add members to groups — only an admin can.\n\n"
                "👉 **Add or unban it manually:** group settings → **Members** → **Add Members** → `@teleaddbyking`\n\n"
                "Then try `/play` again."
            )
            return

        try:
            status = await message.reply(PROCESSING)
        except Exception:
            status = None

        # SPEED: start the voice chat NOW (in parallel with the download) so
        # by the time the track is ready the call is already up. Saves the
        # 2.5s+ propagation wait from the play path.
        call_task = asyncio.create_task(_prestart_call(target_chat))

        try:
            track = await downloader.resolve_track(app, message, query, is_video=is_video)
        except Exception as e:
            txt = f"❌ **Could not load media.**\n\n`{truncate(str(e), 200)}`"
            if status:
                await safe_edit(status, txt, kb.back_to_main())
            else:
                await message.reply(txt)
            call_task.cancel()
            return
        await call_task

        # enqueue & play
        added = await _enqueue(target_chat, track, app, status, message)
        await ctx.DB.bump_stat("video_plays" if is_video else "total_plays")

    # ------------------------------------------------------------------
    # Direct commands: /play, /vplay, /pause, /resume, /skip, /stop, /loop, /volume, /queue
    # ------------------------------------------------------------------
    @app.on_message(filters.command(["play", "vplay"], prefixes=["/", "!"]))
    async def play_cmd(client: Client, message: Message):
        user = message.from_user
        if not user:
            return
        # SOFT-SHUTDOWN GATE
        from handlers.requests import is_bot_offline
        if is_bot_offline() and not guard.is_owner(user.id):
            await message.reply("🛑 **Aura is offline.** Please try again later. 😴")
            return
        if not await guard.can_use_bot(ctx.DB, user.id):
            return
        # ONLY group admins / bot admins / owner may play
        if not await _is_controller(message, alert=False):
            await message.reply(
                "👑 **Admins only!**\n\n"
                "Only group admins (and the bot owner) can play music/videos.\n"
                "Members: send `/request <song name>` and an admin will approve it. 🎧"
            )
            return
        if not is_group(str(message.chat.type)):
            await message.reply(
                "🎧 Use this in a **group** to start streaming!\n\n"
                "Or browse your **💾 Saved library** below — replay any track in a group voice chat:",
                reply_markup=kb.saved_hint_kb(),
            )
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply(
                "🎵 Usage: `/play <song name or link>`\n🎬 Video: `/vplay <song or link>`\n💾 Or pick from the **Saved library**:",
                reply_markup=kb.saved_hint_kb(),
            )
            return
        # boss bot auto-adds the streamer userbot if it's missing from the group
        if not await _ensure_streamer_in_chat(message.chat.id):
            await message.reply(
                "⚠️ The streamer **@teleaddbyking** isn't a member here.\n\n"
                "Telegram doesn't let bots add members to groups — only an admin can.\n\n"
                "👉 **Add or unban it manually:** group settings → **Members** → **Add Members** → `@teleaddbyking`\n\n"
                "Then try `/play` again."
            )
            return
        status = await message.reply(PROCESSING)
        # SPEED: start the call while the track downloads
        call_task = asyncio.create_task(_prestart_call(message.chat.id))
        try:
            track = await downloader.resolve_track(
                app, message, parts[1], is_video=parts[0].lower().startswith("/vplay")
            )
        except Exception as e:
            await safe_edit(status, f"❌ **Could not load media.**\n\n`{truncate(str(e), 200)}`")
            call_task.cancel()
            return
        await call_task
        await _enqueue(message.chat.id, track, app, status, message)
        await ctx.DB.bump_stat("video_plays" if track.is_video else "total_plays")

    @app.on_message(filters.command(["pause"], prefixes=["/", "!"]))
    async def pause_cmd(client: Client, message: Message):
        await _admin_op(message, ctx.STREAMER.pause)

    @app.on_message(filters.command(["resume"], prefixes=["/", "!"]))
    async def resume_cmd(client: Client, message: Message):
        await _admin_op(message, ctx.STREAMER.resume)

    @app.on_message(filters.command(["skip"], prefixes=["/", "!"]))
    async def skip_cmd(client: Client, message: Message):
        await _admin_op(message, _do_skip)

    @app.on_message(filters.command(["stop"], prefixes=["/", "!"]))
    async def stop_cmd(client: Client, message: Message):
        await _admin_op(message, ctx.STREAMER.stop)

    @app.on_message(filters.command(["loop"], prefixes=["/", "!"]))
    async def loop_cmd(client: Client, message: Message):
        if not await _group_only(message):
            return
        if not await _is_controller(message):
            return
        st = manager.get(message.chat.id)
        val = manager.set_loop(message.chat.id, not st.loop)
        await message.reply(f"🔁 Loop is now **{'ON' if val else 'OFF'}**")

    @app.on_message(filters.command(["fw", "forward"], prefixes=["/", "!"]))
    async def fw_cmd(client: Client, message: Message):
        """⏩ Seek forward 10 seconds in the current track."""
        if not await _group_only(message):
            return
        if not await _is_controller(message):
            return
        new_pos = await ctx.STREAMER.seek(message.chat.id, +10)
        if new_pos >= 0:
            await message.reply(f"⏩ **+10s** → now at `{new_pos}s`")
        else:
            await message.reply("🎧 Nothing playing to seek.")

    @app.on_message(filters.command(["fb", "back"], prefixes=["/", "!"]))
    async def fb_cmd(client: Client, message: Message):
        """⏪ Seek back 10 seconds in the current track."""
        if not await _group_only(message):
            return
        if not await _is_controller(message):
            return
        new_pos = await ctx.STREAMER.seek(message.chat.id, -10)
        if new_pos >= 0:
            await message.reply(f"⏪ **-10s** → now at `{new_pos}s`")
        else:
            await message.reply("🎧 Nothing playing to seek.")

    @app.on_message(filters.command(["volume"], prefixes=["/", "!"]))
    async def volume_cmd(client: Client, message: Message):
        if not await _group_only(message):
            return
        if not await _is_controller(message):
            return
        parts = message.text.split()
        try:
            vol = int(parts[1])
        except (IndexError, ValueError):
            vol = config.DEFAULT_VOLUME
        vol = await ctx.STREAMER.set_volume(message.chat.id, vol)
        await message.reply(f"🔊 Volume set to **{vol}**")

    @app.on_message(filters.command(["queue", "q"], prefixes=["/", "!"]))
    async def queue_cmd(client: Client, message: Message):
        if not await _group_only(message):
            return
        await _show_queue(message, page=1)

    @app.on_message(filters.command(["remove", "rm"], prefixes=["/", "!"]))
    async def remove_cmd(client: Client, message: Message):
        """👑 Remove a queued track by its queue number (see /queue)."""
        if not await _group_only(message):
            return
        if not await _is_controller(message):
            return
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply("🎯 Usage: `/remove <queue number>` — see `/queue` for numbers.")
            return
        try:
            idx = int(parts[1])
        except ValueError:
            await message.reply("🎯 Please send a valid queue number, e.g. `/remove 3`.")
            return
        removed = manager.remove_at(message.chat.id, idx)
        if removed:
            await message.reply(f"❌ Removed **#{idx}** — {truncate(removed.title, 60)}")
        else:
            await message.reply(f"⚠️ No track at **#{idx}** in the queue.")

    @app.on_message(filters.command(["leavevc", "stopvc"], prefixes=["/", "!"]))
    async def leavevc_cmd(client: Client, message: Message):
        """👑 Leave the voice chat entirely (ends the call), stay in the group."""
        if not await _group_only(message):
            return
        if not await _is_controller(message):
            return
        await ctx.STREAMER.leave(message.chat.id)
        await message.reply("📴 Left the voice chat. Call ended. Goodbye! 👋")

    @app.on_message(filters.command(["leave", "leavegroup"], prefixes=["/", "!"]))
    async def leavegroup_cmd(client: Client, message: Message):
        """👑 Leave the group entirely: end call, streamer leaves, bot leaves."""
        if not await _group_only(message):
            return
        if not await _is_controller(message):
            return
        chat_id = message.chat.id
        try:
            await ctx.STREAMER.leave(chat_id)  # end the call
        except Exception:
            pass
        try:
            await ctx.USER_APP.leave_chat(chat_id)  # streamer leaves the group
        except Exception:
            pass
        try:
            await message.reply("🚪 Leaving the group. Bye! 👋")
        except Exception:
            pass
        try:
            await client.leave_chat(chat_id)  # bot leaves the group
        except Exception as e:
            logger.warning("leave_chat %s failed: %s", chat_id, e)

    @app.on_message(filters.command(["whitelist", "wl"], prefixes=["/", "!"]))
    async def whitelist_cmd(client: Client, message: Message):
        """✅ Whitelist the current group (or /whitelist <id>) so the bot stays."""
        if not await _is_controller(message):
            return
        parts = message.text.strip().split()
        if len(parts) > 1:
            try:
                chat_id = int(parts[1])
            except ValueError:
                await message.reply("❌ Send a valid group **chat ID**.")
                return
        elif is_group(str(message.chat.type)):
            chat_id = message.chat.id
        else:
            await message.reply("❌ Run this in the group, or send `/whitelist <group id>`.")
            return
        await ctx.DB.whitelist_group(chat_id)
        await message.reply(f"✅ Group `{chat_id}` **whitelisted** — I'll stay now! 🎧")

    # ------------------------------------------------------------------
    # Queue callbacks (pagination / clear / per-track remove)
    # ------------------------------------------------------------------
    @app.on_callback_query(filters.regex(r"^q:"))
    @rate_limited
    async def queue_cb(client: Client, cb: CallbackQuery):
        user = cb.from_user
        if not user or not await guard.can_use_bot(ctx.DB, user.id):
            await cb.answer("🚫 Banned.", show_alert=True)
            return
        action = cb.data.split(":", 1)[1]
        if action == "nop":
            await cb.answer()
            return
        if action == "close":
            try:
                await cb.message.delete()
            except Exception:
                pass
            await cb.answer()
            return
        if not await _is_controller(cb):
            return
        if action == "pg":
            await _show_queue(cb, page=int(cb.data.split(":")[2]))
        elif action == "clear":
            n = manager.clear_queue(cb.message.chat.id)
            await cb.answer(f"🗑️ Removed {n} track(s) from the queue.")
            await _show_queue(cb, page=1)
        elif action == "rm":
            idx = int(cb.data.split(":")[2])
            removed = manager.remove_at(cb.message.chat.id, idx)
            if removed:
                await cb.answer(f"❌ Removed #{idx} — {truncate(removed.title, 40)}")
            else:
                await cb.answer("⚠️ Track not found in the queue.")
            await _show_queue(cb, page=1)

    # ------------------------------------------------------------------
    # Player control callbacks
    # ------------------------------------------------------------------
    @app.on_callback_query(filters.regex(r"^pl:"))
    @rate_limited
    async def player_cb(client: Client, cb: CallbackQuery):
        user = cb.from_user
        if not await guard.can_use_bot(ctx.DB, user.id):
            await cb.answer("🚫 Banned.", show_alert=True)
            return
        if not await _is_controller(cb):
            return
        action = cb.data.split(":", 1)[1]
        chat_id = cb.message.chat.id

        try:
            if action == "pause":
                await ctx.STREAMER.pause(chat_id)
                await _refresh_player(cb.message)
            elif action == "resume":
                await ctx.STREAMER.resume(chat_id)
                await _refresh_player(cb.message)
            elif action == "skip":
                await cb.answer("⏭️ Skipping…")
                await _do_skip(chat_id)
                await _refresh_player(cb.message)
            elif action == "next":
                await cb.answer("⏭️ Next…")
                await _do_skip(chat_id)
                await _refresh_player(cb.message)
            elif action == "stop":
                await ctx.STREAMER.stop(chat_id)
                await safe_edit(
                    cb.message, "⏹️ **Playback stopped.** Queue cleared. See you next time! 🎧", kb.close_only()
                )
            elif action == "vol":
                st = manager.get(chat_id)
                await safe_edit(
                    cb.message,
                    f"🔊 **Volume Control**\n\nCurrent: **{st.volume}**",
                    kb.volume_kb(st.volume),
                )
            elif action == "loop":
                st = manager.get(chat_id)
                val = manager.set_loop(chat_id, not st.loop)
                await cb.answer(f"🔁 Loop {'ON' if val else 'OFF'}")
                await _refresh_player(cb.message)
            elif action == "seekb":
                new_pos = await ctx.STREAMER.seek(chat_id, -10)
                if new_pos >= 0:
                    await cb.answer(f"⏪ -10s → {new_pos}s")
                else:
                    await cb.answer("🎧 Nothing playing to seek.", show_alert=True)
                await _refresh_player(cb.message)
            elif action == "seekf":
                new_pos = await ctx.STREAMER.seek(chat_id, +10)
                if new_pos >= 0:
                    await cb.answer(f"⏩ +10s → {new_pos}s")
                else:
                    await cb.answer("🎧 Nothing playing to seek.", show_alert=True)
                await _refresh_player(cb.message)
            elif action == "queue":
                await _show_queue(cb, page=1)
            elif action == "leave":
                await ctx.STREAMER.leave(chat_id)
                await safe_edit(
                    cb.message, "📴 **Left the voice chat.** Call ended. Goodbye! 👋", kb.close_only()
                )
            elif action == "close":
                try:
                    await cb.message.delete()
                except Exception:
                    pass
                await cb.answer("Player closed")
        except Exception as e:
            if "not in a call" in str(e).lower():
                await safe_edit(
                    cb.message, "🎧 **Nothing is playing right now.** Start something first!", kb.close_only()
                )
            else:
                logger.warning("player_cb %s failed: %s", action, e)
                try:
                    await cb.answer(f"❌ {truncate(str(e), 80)}", show_alert=True)
                except Exception:
                    pass

    @app.on_callback_query(filters.regex(r"^vol:"))
    @rate_limited
    async def volume_cb(client: Client, cb: CallbackQuery):
        if not await _is_controller(cb):
            return
        action = cb.data.split(":", 1)[1]
        chat_id = cb.message.chat.id
        st = manager.get(chat_id)
        if action == "nop":
            await cb.answer()
        elif action == "-10":
            vol = await ctx.STREAMER.set_volume(chat_id, st.volume - 10)
            await cb.answer(f"🔉 Volume: {vol}")
            await safe_edit(cb.message, f"🔊 **Volume Control**\n\nCurrent: **{vol}**", kb.volume_kb(vol))
        elif action == "+10":
            vol = await ctx.STREAMER.set_volume(chat_id, st.volume + 10)
            await cb.answer(f"🔊 Volume: {vol}")
            await safe_edit(cb.message, f"🔊 **Volume Control**\n\nCurrent: **{vol}**", kb.volume_kb(vol))
        elif action == "mute":
            await ctx.STREAMER.set_volume(chat_id, 0)
            await cb.answer("🔇 Muted")
            await safe_edit(cb.message, "🔇 **Muted**", kb.volume_kb(0))
        elif action == "unmute":
            vol = await ctx.STREAMER.set_volume(chat_id, config.DEFAULT_VOLUME)
            await cb.answer("🔈 Unmuted")
            await safe_edit(cb.message, f"🔊 **Volume Control**\n\nCurrent: **{vol}**", kb.volume_kb(vol))
        elif action == "back":
            await _refresh_player(cb.message)

    # ------------------------------------------------------------------
    # Saved library (💾) — every played track, replayable
    # ------------------------------------------------------------------
    @app.on_callback_query(filters.regex(r"^sv:"))
    @rate_limited
    async def saved_cb(client: Client, cb: CallbackQuery):
        user = cb.from_user
        if not user:
            return
        # SOFT-SHUTDOWN GATE
        from handlers.requests import is_bot_offline
        if is_bot_offline() and not guard.is_owner(user.id):
            await cb.answer("🛑 Aura is offline. 😴", show_alert=True)
            return
        if not await guard.can_use_bot(ctx.DB, user.id):
            await cb.answer("🚫 Banned.", show_alert=True)
            return
        parts = cb.data.split(":")
        action = parts[1]

        if action == "nop":
            await cb.answer()
            return
        if action == "pg":
            await show_saved(cb, page=int(parts[2]))
            return
        if action == "del":
            # owner-only: delete a saved track from the library
            if not guard.is_owner(user.id):
                await cb.answer("👑 Only the owner can delete saved tracks.", show_alert=True)
                return
            track_id = int(parts[2])
            row = await ctx.DB.get_saved_track(track_id)
            if not row:
                await cb.answer("Track already gone.", show_alert=True)
                return
            removed = await ctx.DB.delete_saved_track(track_id)
            if not removed:
                await cb.answer("Track already gone.", show_alert=True)
                return
            await cb.answer("🗑️ Deleted.")
            # re-render the library on the current page
            page = int(parts[3]) if len(parts) > 3 else 1
            await show_saved(cb, page=page)
            return
        if action != "play":
            return

        # ONLY group admins / bot admins / owner may play from Saved
        if not await _is_controller(cb, alert=False):
            await cb.answer("👑 Admins only! Members use /request.", show_alert=True)
            return

        if not is_group(str(cb.message.chat.type)):
            await cb.answer("🔊 Open a **group voice chat**, then use 💾 Saved there!", show_alert=True)
            return

        if not await _ensure_streamer_in_chat(cb.message.chat.id):
            await cb.answer("⚠️ Couldn't add the streamer to this group. Make the bot admin!", show_alert=True)
            return

        track_id = int(parts[2])
        row = await ctx.DB.get_saved_track(track_id)
        if not row:
            await cb.answer("Track not found.", show_alert=True)
            return
        await cb.answer("⏳ Loading…")
        status = await cb.message.reply(PROCESSING)
        chat_id = cb.message.chat.id
        try:
            track = await _resolve_saved(row)
        except Exception as e:
            await safe_edit(status, f"❌ **Could not load saved track.**\n\n`{truncate(str(e), 150)}`", kb.close_only())
            return
        await _enqueue(chat_id, track, client, status, cb.message)
        await ctx.DB.bump_stat("video_plays" if track.is_video else "total_plays")


# ----------------------------------------------------------------------
async def _enqueue(chat_id: int, track: Track, app, status: Message, message: Message):
    """Add track to queue; start playback if idle. Returns position."""
    st = manager.get(chat_id)
    if st.playing:
        # stale-state guard: if we THINK we're playing but no voice chat is
        # actually live (call ended silently / update missed), reset the state
        # and start fresh instead of queueing behind a dead player
        if not await ctx.STREAMER._call_active(chat_id):
            logger.warning("stale playing state in %s — resetting, starting fresh", chat_id)
            manager.stop(chat_id)
        else:
            pos = manager.add_track(chat_id, track)
            await _notify_queued(status, track, pos)
            # SMART: a stream is already going — let the chat know who added
            # the next track (admin/member) so nobody is surprised by a takeover
            requester_name = track.requester_name or "Someone"
            smart_txt = (
                f"🎧 **Up next** — added by **{requester_name}**\n"
                f"🎵 {truncate(track.title, 60)}\n"
                f"⏱ {format_duration(track.duration)} · position **#{pos}**"
            )
            await _chat_notice(chat_id, smart_txt, status)
            return pos

    # start playback
    started = await ctx.STREAMER.play_track(chat_id, track)
    if not started:
        await safe_edit(status, "❌ Could not start playback. Try again.")
        return 0

    st = manager.get(chat_id)
    st.player_msg = status
    txt = _now_playing_text(st)
    await safe_edit(status, txt, kb.player_kb(manager.state_snapshot(chat_id)))

    # auto-delete the request message if configured — BUT never delete a
    # user's actual media file (audio/video/document they sent); that would
    # erase their file from the chat. Only text requests get auto-deleted.
    if config.AUTO_DELETE_MS and not (
        message.audio or message.video
        or (message.document and message.document.mime_type)
    ):
        asyncio.create_task(_auto_delete(message, config.AUTO_DELETE_MS))
    return 1


async def _notify_queued(status: Message, track: Track, pos: int):
    txt = (
        f"✅ **Added to queue** (#**{pos}**)\n\n"
        f"🎵 **{truncate(track.title, 60)}**\n"
        f"⏱ {format_duration(track.duration)} · 👤 {track.requester_name}"
    )
    await safe_edit(status, txt, kb.close_only())


async def _chat_notice(chat_id: int, txt: str, status: Message | None):
    """Send a notice to the chat (falls back to editing the status message)."""
    try:
        if status is not None:
            await safe_edit(status, txt, kb.close_only())
        else:
            await ctx.BOT_APP.send_message(chat_id, txt)
    except Exception as e:
        logger.warning("chat_notice %s failed: %s", chat_id, e)


async def _prestart_call(chat_id: int):
    """Start/verify the group voice chat ASAP (run in parallel with downloads).

    Never raises — if the call can't start here, _safe_play's own
    _ensure_call + auto_start fallback still handle it.
    """
    try:
        await ctx.STREAMER._ensure_call(chat_id)
    except Exception as e:
        logger.debug("prestart_call %s: %s", chat_id, e)


def _now_playing_text(st) -> str:
    t = st.current
    if not t:
        return "🎧 Nothing playing."
    kind = "🎬" if t.is_video else "🎵"
    dur = format_duration(t.duration) if t.duration else "live/unknown"
    loop = "🔁 ON" if st.loop else "OFF"
    pos_txt = f" · 📍 {format_duration(st.seek_pos)}" if st.seek_pos else ""
    return (
        f"{kind} **Now Playing**\n\n"
        f"**{truncate(t.title, 70)}**\n"
        f"⏱ {dur}{pos_txt} · 🔊 Vol {st.volume} · 🔁 {loop}\n"
        f"👤 Requested by: {t.requester_name or '—'}"
    )


async def _refresh_player(message: Message):
    st = manager.get(message.chat.id)
    if not st.playing:
        await safe_edit(message, "🎧 Playback ended.", kb.close_only())
        return
    await safe_edit(message, _now_playing_text(st), kb.player_kb(manager.state_snapshot(message.chat.id)))


async def _do_skip(chat_id: int):
    from player import downloader as dl

    old = None
    try:
        old = ctx.STREAMER._playing_file.pop(chat_id, None)
    except Exception:
        pass
    dl.delete_file(old)
    nxt = manager.next_track(chat_id, force=True)
    if nxt is None:
        manager.set_current(chat_id, None)
        await ctx.STREAMER.stop(chat_id)
        return
    manager.set_current(chat_id, nxt)
    await ctx.STREAMER._safe_play(chat_id, nxt)


async def _show_queue(cb_or_msg, page: int = 1):
    chat_id = cb_or_msg.message.chat.id if isinstance(cb_or_msg, CallbackQuery) else cb_or_msg.chat.id
    st = manager.get(chat_id)
    total = len(st.queue)
    if total == 0 and not st.current:
        txt = "📜 **Queue is empty.**\n\nTap **🎵 Play Music** to add songs!"
        markup = kb.close_only()
    else:
        page = max(1, min(page, max(1, (total + 4) // 5)))
        items = st.queue[(page - 1) * 5: page * 5]
        lines = []
        if st.current:
            lines.append(f"{'🎬' if st.current.is_video else '🎵'} **Now:** {truncate(st.current.title, 40)}")
            lines.append("")
        for i, t in enumerate(items, start=(page - 1) * 5 + 1):
            lines.append(f"`{i}.` {'🎬' if t.is_video else '🎵'} {truncate(t.title, 35)} — {format_duration(t.duration)}")
        if total == 0:
            lines.append("_(no upcoming tracks)_")
        pages = max(1, (total + 4) // 5)
        admin = await _is_controller(cb_or_msg, alert=False)
        btn_items = [(i, t.title) for i, t in enumerate(items, start=(page - 1) * 5 + 1)]
        txt = f"📜 **Queue** ({total} upcoming)\n\n" + "\n".join(lines)
        if admin and items:
            txt += "\n\n_👑 Tap ❌ to remove a track._"
        markup = kb.queue_kb(page, pages, admin, btn_items)
    if isinstance(cb_or_msg, CallbackQuery):
        await safe_edit(cb_or_msg.message, txt, markup)
    else:
        await cb_or_msg.reply(txt, reply_markup=markup)


async def _auto_delete(message: Message, delay_ms: int):
    await asyncio.sleep(delay_ms / 1000)
    try:
        await message.delete()
    except Exception:
        pass


_streamer_ok_chats: set = set()
_promote_warned: set = set()


async def _ensure_streamer_in_chat(chat_id: int) -> bool:
    """Make sure the streaming userbot is a member AND admin of the group.

    If it is missing, the bot auto-adds it (invite-link fallback), then
    ALWAYS promotes it (manage voice chats) so it can start the group call.
    Returns True on success.
    """
    if chat_id in _streamer_ok_chats:
        return True
    is_member = False
    try:
        member = await ctx.USER_APP.get_chat_member(chat_id, "me")
        status = str(member.status).split(".")[-1].lower()
        if status in ("member", "administrator", "creator"):
            is_member = True
    except Exception:
        # cold peer cache (fresh process) — prime the chat, then re-check
        try:
            await ctx.USER_APP.get_chat(chat_id)
            member = await ctx.USER_APP.get_chat_member(chat_id, "me")
            status = str(member.status).split(".")[-1].lower()
            if status in ("member", "administrator", "creator"):
                is_member = True
        except Exception:
            pass  # userbot not a member (or peer unreachable)
    # Only auto-add inside groups/supergroups
    try:
        chat = await ctx.BOT_APP.get_chat(chat_id)
        if str(chat.type).split(".")[-1].lower() not in ("group", "supergroup"):
            _streamer_ok_chats.add(chat_id)
            return True
    except Exception:
        return False
    if not is_member:
        try:
            try:
                await ctx.BOT_APP.add_chat_members(chat_id, ctx.USER_APP.me.id)
            except Exception as e:
                # bots can't add members to supergroups (BOT_METHOD_INVALID) —
                # try an invite-link join (works unless the streamer is banned)
                logger.warning("add_chat_members failed in %s (%s) — trying invite link", chat_id, e)
                try:
                    link = await ctx.BOT_APP.create_chat_invite_link(chat_id, member_limit=1)
                    try:
                        await ctx.USER_APP.join_chat(link.invite_link)
                    except Exception as join_e:
                        if "USER_ALREADY_PARTICIPANT" in str(join_e):
                            # already a member — that's success, keep going
                            logger.info("streamer already a member in %s — continuing", chat_id)
                        else:
                            # INVITE_HASH_EXPIRED on a fresh link usually means the
                            # streamer is banned — unban via the bot admin, then rejoin
                            logger.warning("streamer join failed — trying unban + rejoin in %s", chat_id)
                            try:
                                await ctx.BOT_APP.unban_chat_member(chat_id, ctx.USER_APP.me.id)
                                await asyncio.sleep(0.5)
                                await ctx.USER_APP.join_chat(link.invite_link)
                            except Exception:
                                raise
                except Exception as e2:
                    logger.warning("invite-link join failed in %s: %s", chat_id, e2)
                    raise
            await asyncio.sleep(0.5)
            # prime the userbot's peer cache so PyTgCalls can interact with the chat
            primed = False
            for _ in range(3):
                try:
                    await ctx.USER_APP.get_chat(chat_id)
                    primed = True
                    break
                except Exception:
                    await asyncio.sleep(0.5)
            if not primed:
                return False
        except Exception as e:
            logger.warning("auto-add streamer failed in %s: %s", chat_id, e)
            return False
    # Ensure admin — needed to create/start the group call.
    # If the streamer is ALREADY an admin, promotion is unnecessary (and the
    # bot may even lack the right to promote) — only promote plain members.
    try:
        cur = await ctx.BOT_APP.get_chat_member(chat_id, ctx.USER_APP.me.id)
        cur_status = str(cur.status).split(".")[-1].lower()
    except Exception:
        cur_status = ""
    if cur_status in ("administrator", "creator"):
        logger.info("streamer already admin in %s — no promote needed", chat_id)
    else:
        try:
            await ctx.BOT_APP.promote_chat_member(
                chat_id,
                ctx.USER_APP.me.id,
                privileges=ChatPrivileges(
                    can_manage_video_chats=True,  # covers voice + video calls
                    can_invite_users=True,
                ),
            )
            await asyncio.sleep(0.3)
            try:
                member = await ctx.BOT_APP.get_chat_member(chat_id, ctx.USER_APP.me.id)
                if str(member.status).split(".")[-1].lower() != "administrator":
                    logger.warning("streamer NOT admin after promote in %s — bot may lack 'manage voice chats' right", chat_id)
            except Exception:
                pass
        except Exception as e:
            logger.warning("promote failed in %s: %s", chat_id, e)
            if chat_id not in _promote_warned:
                _promote_warned.add(chat_id)
                try:
                    await ctx.BOT_APP.send_message(
                        chat_id,
                        "⚠️ I can't promote the streamer to admin here — make **me an admin** "
                        "in this group for full auto features (auto-start + auto-end of the call).",
                    )
                except Exception:
                    pass
    _streamer_ok_chats.add(chat_id)
    return True


async def _is_controller(obj, alert: bool = True) -> bool:
    """Player controls = bot owner + bot admins + GROUP admins only.

    `alert=False` makes it silent (used when deciding whether to SHOW admin buttons).
    SOFT-SHUTDOWN: while offline, only the owner controls anything.
    """
    user = obj.from_user
    if not user:
        return False
    from handlers.requests import is_bot_offline
    if is_bot_offline() and not guard.is_owner(user.id):
        if alert:
            try:
                await obj.answer("🛑 Aura is offline. 😴", show_alert=True)
            except Exception:
                pass
        return False
    if await guard.is_admin(ctx.DB, user.id):
        return True
    # group admin check (supergroup/group/channel — channels with
    # discussion are also playable venues)
    chat = getattr(obj, "chat", None) or getattr(getattr(obj, "message", None), "chat", None)
    if chat and getattr(chat, "type", None):
        ctype = str(chat.type).split(".")[-1].lower()
        if ctype in ("group", "supergroup", "channel"):
            try:
                member = await chat.get_member(user.id)
                st = str(member.status).split(".")[-1].lower()
                if st in ("administrator", "creator", "owner"):
                    return True
            except Exception as e:
                logger.debug("get_member(%s) in %s failed: %s", user.id, chat.id, e)
                # fallback: try via bot client with resolved peer
                try:
                    member = await ctx.BOT_APP.get_chat_member(chat.id, user.id)
                    st = str(member.status).split(".")[-1].lower()
                    if st in ("administrator", "creator", "owner"):
                        return True
                except Exception:
                    pass
    if alert:
        try:
            await obj.answer("👑 Group admins only can control the player!", show_alert=True)
        except Exception:
            pass
    return False


async def _admin_op(message: Message, op):
    """Run an admin-only player op; reply with the result."""
    if not await _group_only(message):
        return
    if not await _is_controller(message):
        return
    chat_id = message.chat.id
    try:
        await op(chat_id)
        name = getattr(op, "__name__", str(op))
        emoji = {"pause": "⏸️", "resume": "▶️", "stop": "⏹️", "skip": "⏭️"}.get(name, "✅")
        await message.reply(f"{emoji} **{name.capitalize()}** done.")
    except Exception as e:
        await message.reply(f"❌ Failed: `{truncate(str(e), 100)}`")


# ----------------------------------------------------------------------
# Saved library (💾)
# ----------------------------------------------------------------------
SAVED_PER_PAGE = 6


async def show_saved(cb_or_msg, page: int = 1):
    """Render the saved-track library (paginated list of playable buttons)."""
    total = await ctx.DB.count_saved_tracks()
    if total == 0:
        txt = "💾 **No saved tracks yet.**\n\nEvery music/video played will show up here, so anyone can replay it later."
        markup = kb.back_to_main()
    else:
        pages = max(1, (total + SAVED_PER_PAGE - 1) // SAVED_PER_PAGE)
        page = max(1, min(page, pages))
        items = await ctx.DB.saved_tracks_page(limit=SAVED_PER_PAGE, offset=(page - 1) * SAVED_PER_PAGE)
        lines = []
        for i, r in enumerate(items, start=(page - 1) * SAVED_PER_PAGE + 1):
            dur = format_duration(r["duration"]) if r["duration"] else "?"
            icon = "🎬" if r["is_video"] else "🎵"
            lines.append(f"`{i}.` {icon} {truncate(r['title'], 42)} — {dur} · ▶️ {r['plays']}x")
        # who is viewing? owner sees the 🗑️ delete buttons
        viewer = cb_or_msg.from_user if isinstance(cb_or_msg, CallbackQuery) else getattr(cb_or_msg, "from_user", None)
        is_owner = bool(viewer and guard.is_owner(viewer.id))
        txt = (
            f"💾 **Saved Library** ({total})\n\n"
            + "\n".join(lines)
            + ("\n\n_Tap a track to play it. 🗑️ deletes it._" if is_owner else "\n\n_Tap a track to play it in this group._")
        )
        markup = kb.saved_kb(items, page, pages, is_owner=is_owner)
    if isinstance(cb_or_msg, CallbackQuery):
        await safe_edit(cb_or_msg.message, txt, markup)
    else:
        await cb_or_msg.reply(txt, reply_markup=markup)


async def _resolve_saved(row) -> Track:
    """Turn a saved_tracks row back into a playable Track."""
    if row["file_id"]:
        path = await ctx.BOT_APP.download_media(row["file_id"])
        if not path:
            raise RuntimeError("Could not re-download the saved file.")
        return Track(
            title=row["title"],
            duration=row["duration"],
            file_path=path,
            source="telegram",
            is_video=bool(row["is_video"]),
            file_id=row["file_id"],
        )
    url = row["url"] or row["source"]
    return await downloader.resolve_url(
        url,
        is_video=bool(row["is_video"]),
        requester_name="💾 Saved",
    )
