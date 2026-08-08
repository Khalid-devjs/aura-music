"""Music/video playback flow + player control buttons + queue."""
import asyncio
import logging

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

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
        if not user or not await guard.can_use_bot(ctx.DB, user.id):
            return
        req = ctx.pending.pop(user.id)
        if not req:
            # no ctx.pending request — but still register the user
            await ctx.DB.add_user(user.id, user.username or "", user.first_name or "")
            return
        action = req["action"]
        target_chat = req["data"].get("chat_id") or message.chat.id

        if action not in ("play_music", "play_video"):
            return
        is_video = action == "play_video"

        # a Telegram media file?
        has_media = bool(
            message.audio or message.video or (message.document and message.document.mime_type)
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

        try:
            status = await message.reply(PROCESSING)
        except Exception:
            status = None

        try:
            track = await downloader.resolve_track(app, message, query, is_video=is_video)
        except Exception as e:
            txt = f"❌ **Could not load media.**\n\n`{truncate(str(e), 200)}`"
            if status:
                await safe_edit(status, txt, kb.back_to_main())
            else:
                await message.reply(txt)
            return

        # enqueue & play
        added = await _enqueue(target_chat, track, app, status, message)
        await ctx.DB.bump_stat("video_plays" if is_video else "total_plays")

    # ------------------------------------------------------------------
    # Direct commands: /play, /vplay, /pause, /resume, /skip, /stop, /loop, /volume, /queue
    # ------------------------------------------------------------------
    @app.on_message(filters.command(["play", "vplay"], prefixes=["/", "!"]))
    async def play_cmd(client: Client, message: Message):
        user = message.from_user
        if not user or not await guard.can_use_bot(ctx.DB, user.id):
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
        status = await message.reply(PROCESSING)
        try:
            track = await downloader.resolve_track(
                app, message, parts[1], is_video=parts[0].lower().startswith("/vplay")
            )
        except Exception as e:
            await safe_edit(status, f"❌ **Could not load media.**\n\n`{truncate(str(e), 200)}`")
            return
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
        if not await _is_controller(message):
            return
        st = manager.get(message.chat.id)
        val = manager.set_loop(message.chat.id, not st.loop)
        await message.reply(f"🔁 Loop is now **{'ON' if val else 'OFF'}**")

    @app.on_message(filters.command(["volume"], prefixes=["/", "!"]))
    async def volume_cmd(client: Client, message: Message):
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
        await _show_queue(app, message, page=1)

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
            elif action == "queue":
                await _show_queue(cb, page=1)
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
        if not user or not await guard.can_use_bot(ctx.DB, user.id):
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
        if action != "play":
            return

        if not is_group(str(cb.message.chat.type)):
            await cb.answer("🔊 Open a **group voice chat**, then use 💾 Saved there!", show_alert=True)
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
        pos = manager.add_track(chat_id, track)
        await _notify_queued(status, track, pos)
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

    # auto-delete the request message if configured
    if config.AUTO_DELETE_MS:
        asyncio.create_task(_auto_delete(message, config.AUTO_DELETE_MS))
    return 1


async def _notify_queued(status: Message, track: Track, pos: int):
    txt = (
        f"✅ **Added to queue** (#**{pos}**)\n\n"
        f"🎵 **{truncate(track.title, 60)}**\n"
        f"⏱ {format_duration(track.duration)} · 👤 {track.requester_name}"
    )
    await safe_edit(status, txt, kb.close_only())


def _now_playing_text(st) -> str:
    t = st.current
    if not t:
        return "🎧 Nothing playing."
    kind = "🎬" if t.is_video else "🎵"
    dur = format_duration(t.duration) if t.duration else "live/unknown"
    loop = "🔁 ON" if st.loop else "OFF"
    return (
        f"{kind} **Now Playing**\n\n"
        f"**{truncate(t.title, 70)}**\n"
        f"⏱ {dur} · 🔊 Vol {st.volume} · 🔁 {loop}\n"
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
        admin = await guard.is_admin(ctx.DB, cb_or_msg.from_user.id)
        txt = f"📜 **Queue** ({total} upcoming)\n\n" + "\n".join(lines)
        markup = kb.queue_kb(page, pages, admin)
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


async def _is_controller(obj) -> bool:
    """Player controls are admin/owner-only."""
    user = obj.from_user
    if not user:
        return False
    if await guard.is_admin(ctx.DB, user.id):
        return True
    try:
        await obj.answer("👑 Admins only can control the player!", show_alert=True)
    except Exception:
        pass
    return False


async def _admin_op(message: Message, op):
    """Run an admin-only player op; reply with the result."""
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
        txt = (
            f"💾 **Saved Library** ({total})\n\n"
            + "\n".join(lines)
            + "\n\n_Tap a track to play it in this group._"
        )
        markup = kb.saved_kb(items, page, pages)
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
