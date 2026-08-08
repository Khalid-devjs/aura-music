"""Welcome / help / main menu."""
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import config
from buttons import inline as kb
from handlers import context as ctx
from modules import filters as guard
from modules.helpers import log_event, safe_edit
from modules.ratelimit import rate_limited
from utils.formatters import format_duration

WELCOME = (
    "🎧 **Welcome to {name}!**\n\n"
    "I stream **music** and **videos** straight into group voice chats.\n"
    "No downloads needed — just send a song name, a YouTube link, or a media file.\n\n"
    "**Quick start:**\n"
    "▸ Add me to a group\n"
    "▸ Open the group's voice chat\n"
    "▸ Tap **🎵 Play Music** and send a song\n\n"
    "**Made with ❤️ — buttons only, no commands needed.**"
)

HELP = (
    "📜 **Help Center**\n\n"
    "**🎵 Music** — tap Play Music, then send a song name, YouTube link, or audio file.\n"
    "**🎬 Video** — same, but streams video in the voice chat.\n"
    "**⏸️ Pause / ▶️ Resume** — pause and resume playback.\n"
    "**⏭️ Skip** — skip to the next track in queue.\n"
    "**⏹️ Stop** — stop playback and clear the queue.\n"
    "**🔊 Volume** — adjust the stream volume.\n"
    "**📜 Queue** — see what's playing and what's up next.\n"
    "**🔁 Loop** — repeat the current track.\n\n"
    "**Commands (work too):**\n"
    "`/start` `/help` `/play <song>` `/vplay <song>` `/pause` `/resume` `/skip` `/stop` `/queue` `/volume 80` `/loop`\n\n"
    "**Permissions:** only admins control the player. Everyone can queue songs."
)

STATS_TMPL = (
    "📊 **Bot Statistics**\n\n"
    "👥 **Users:** {users}\n"
    "👥 **Groups:** {groups}\n"
    "▶️ **Total Plays:** {plays}\n"
    "🎬 **Videos Played:** {videos}\n"
    "📢 **Broadcasts:** {broadcasts}\n"
    "⏱ **Uptime:** {uptime}\n"
    "⚡ **Active Calls:** {active}"
)

DEV = (
    "👨‍💻 **Developer**\n\n"
    "**Aura Music Bot** — a premium Telegram voice-chat music & video streamer.\n\n"
    "⚙️ **Engine:** Pyrogram + PyTgCalls + FFmpeg\n"
    "🗄️ **Database:** SQLite (async)\n"
    "🎛️ **UI:** Full button interface\n\n"
    "📢 **Support:** {support}\n"
    "🔗 **Version:** 1.0.0"
)


def register(app: Client) -> None:
    @app.on_message(filters.command("start", prefixes=["/", "!"]))
    async def start_cmd(client: Client, message: Message):
        user = message.from_user
        if user and await guard.can_use_bot(ctx.DB, user.id):
            await ctx.DB.add_user(user.id, user.username or "", user.first_name or "")
        admin = bool(user and await guard.is_admin(ctx.DB, user.id))
        txt = WELCOME.format(name=config.BOT_NAME, support=config.SUPPORT_CHAT or "—")
        await message.reply(txt, reply_markup=kb.main_menu(admin))

    @app.on_message(filters.command("help", prefixes=["/", "!"]))
    async def help_cmd(client: Client, message: Message):
        await message.reply(HELP, reply_markup=kb.back_to_main())

    @app.on_message(filters.command("stats", prefixes=["/", "!"]))
    async def stats_cmd(client: Client, message: Message):
        await _send_stats(message)

    # ------------------------------------------------------------------
    @app.on_callback_query(filters.regex(r"^main:"))
    @rate_limited
    async def main_menu_cb(client: Client, cb: CallbackQuery):
        user = cb.from_user
        if not await guard.can_use_bot(ctx.DB, user.id):
            await cb.answer("🚫 You are banned.", show_alert=True)
            return
        action = cb.data.split(":", 1)[1]

        if action == "music":
            await cb.answer()
            await safe_edit(cb.message, "🎵 **Send me a song!**\n\nYou can send:\n▸ A song name (e.g. `Burna Boy - Last Last`)\n▸ A YouTube link\n▸ An audio file\n\n_(150s timeout)_", kb.back_to_main())
            ctx.pending.set(user.id, "play_music", chat_id=cb.message.chat.id)
            return

        if action == "video":
            await cb.answer()
            await safe_edit(cb.message, "🎬 **Send me a video!**\n\nYou can send:\n▸ A song name (e.g. `Sarkodie - Country`)\n▸ A YouTube link\n▸ A video file\n\n_(150s timeout)_", kb.back_to_main())
            ctx.pending.set(user.id, "play_video", chat_id=cb.message.chat.id)
            return

        if action == "help":
            await cb.answer()
            await safe_edit(cb.message, HELP, kb.back_to_main())
            return

        if action == "settings":
            await cb.answer()
            autodel = (await ctx.DB.get_setting("auto_delete_ms", str(config.AUTO_DELETE_MS))) != "0"
            await safe_edit(cb.message, "⚙️ **Settings**", kb.settings_menu(autodel))
            return

        if action == "stats":
            await cb.answer()
            await _send_stats(cb.message)
            return

        if action == "saved":
            await cb.answer()
            from handlers.player import show_saved  # local import avoids cycles

            await show_saved(cb, page=1)
            return

        if action == "dev":
            await cb.answer()
            await safe_edit(
                cb.message,
                DEV.format(support=config.SUPPORT_CHAT or "—"),
                kb.back_to_main(),
            )
            return

        if action == "admin":
            await cb.answer()
            if not await guard.is_admin(ctx.DB, user.id):
                await cb.answer("👑 Admins only!", show_alert=True)
                return
            await safe_edit(cb.message, "👑 **Admin Panel**", kb.admin_menu())
            return

        if action == "back":
            await cb.answer()
            admin = await guard.is_admin(ctx.DB, user.id)
            await safe_edit(cb.message, WELCOME.format(name=config.BOT_NAME), kb.main_menu(admin))
            return

        if action == "close":
            try:
                await cb.message.delete()
            except Exception:
                pass
            await cb.answer("Closed", show_alert=False)


async def _send_stats(message: Message):
    users = await ctx.DB.count_users()
    groups = await ctx.DB.count_groups()
    plays = await ctx.DB.get_stat("total_plays")
    videos = await ctx.DB.get_stat("video_plays")
    broadcasts = await ctx.DB.get_stat("broadcasts")
    import time

    uptime = int(time.time() - ctx.START_TIME)
    active = len(ctx.STREAMER.active_calls()) if ctx.STREAMER else 0
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    txt = STATS_TMPL.format(
        users=users,
        groups=groups,
        plays=plays,
        videos=videos,
        broadcasts=broadcasts,
        uptime=f"{h}h {m}m {s}s",
        active=active,
    )
    if isinstance(message, CallbackQuery):
        await safe_edit(message.message, txt, kb.back_to_main())
    else:
        await message.reply(txt, reply_markup=kb.back_to_main())
