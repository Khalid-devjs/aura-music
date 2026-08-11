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

WELCOME_OWNER = (
    "👑 **Hey boss, {first}!** I know you — you're my **owner**. 😎\n\n"
    "I see a special **🔒 Owner Secret** button in the menu below.\n"
    "It hides your secret commands (`/shutdown`, `/kaboom`) so you can\n"
    "switch me off and on whenever you want — and no one else can touch it.\n\n"
    "I'm fully alive and streaming. Ready when you are, boss. 🎧✨"
)

HELP = (
    "📜 **Help Center — Aura Music**\n\n"
    "**🎧 How to play music**\n"
    "1. Add me to a **group**.\n"
    "2. Open the group's **voice chat**.\n"
    "3. Tap **🎵 Play Music** and send a song name, YouTube link, or audio file.\n\n"
    "**🎬 How to play videos**\n"
    "Same, but tap **🎬 Play Video** — streams the video in the voice chat.\n\n"
    "**💾 Saved Library**\n"
    "Every track played is auto-saved. Tap **💾 Saved** to replay anything anytime.\n\n"
    "**🎮 Player controls (group admins only)**\n"
    "▸ ⏸️ Pause / ▶️ Resume / ⏭️ Skip / ⏹️ Stop\n"
    "▸ 🔊 Volume — tap and use the slider buttons\n"
    "▸ 🔁 Loop — repeat the current track\n"
    "▸ 📜 Queue — see what's playing & remove tracks (admin)\n\n"
    "**📨 Song requests (members)**\n"
    "Members can send `/request <song name>` — a group admin gets notified and\n"
    "**approves** it, then it plays. Admins: see pending with `/requests`.\n\n"
    "**⌨️ Commands**\n"
    "`/start` `/help` `/play <song>` `/vplay <song>` `/pause` `/resume`\n"
    "`/skip` `/stop` `/queue` `/volume 80` `/loop` `/request <song>` `/requests`\n\n"
    "**👑 Admin access**\n"
    "Player controls + request approval = **group admins** (and bot admins).\n"
    "The **Admin Panel** button (👑) appears for admins in the menu.\n\n"
    "**🔒 Owner area**\n"
    "The owner sees a special **🔒 Owner Secret** button with hidden commands.\n\n"
    "**Need help?** Contact: {support}"
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
    "🔗 **Version:** {version}"
)

LOGO = "assets/logo.jpg"


async def _with_logo(reply_fn, caption: str, markup):
    """Send the bot logo as a photo with the given caption (falls back to plain text)."""
    try:
        return await reply_fn(LOGO, caption=caption, reply_markup=markup)
    except Exception:
        return await reply_fn(caption, reply_markup=markup)


def register(app: Client) -> None:
    @app.on_message(filters.command("start", prefixes=["/", "!"]))
    async def start_cmd(client: Client, message: Message):
        user = message.from_user
        if user and await guard.can_use_bot(ctx.DB, user.id):
            await ctx.DB.add_user(user.id, user.username or "", user.first_name or "")
        admin = bool(user and await guard.is_admin(ctx.DB, user.id))
        owner = bool(user and guard.is_owner(user.id))
        # "Aura knows me as its owner" — special greeting for the owner
        if owner:
            txt = WELCOME_OWNER.format(
                name=config.BOT_NAME,
                first=user.first_name or "Owner",
                support=config.SUPPORT_CHAT or "—",
            )
        else:
            txt = WELCOME.format(name=config.BOT_NAME, support=config.SUPPORT_CHAT or "—")
        await _with_logo(message.reply_photo, txt, kb.main_menu(admin, is_owner=owner))

    @app.on_message(filters.command("help", prefixes=["/", "!"]))
    async def help_cmd(client: Client, message: Message):
        await _with_logo(
            message.reply_photo,
            HELP.format(support=config.SUPPORT_CHAT or "—"),
            kb.back_to_main(),
        )

    @app.on_message(filters.command("stats", prefixes=["/", "!"]))
    async def stats_cmd(client: Client, message: Message):
        await _send_stats(message)

    # /admin — command-only admin panel (buttons removed from main menu,
    # user-mandated 2026-08-11: "buttons are too much looks weird").
    @app.on_message(filters.command("admin", prefixes=["/", "!"]) & filters.private)
    async def admin_cmd(client: Client, message: Message):
        user = message.from_user
        if not user:
            return
        if not await guard.is_admin(ctx.DB, user.id):
            await message.reply_text("🚫 *Access denied.* You are not authorized to use this command.")
            return
        await message.reply_text("👑 **Admin Panel**", reply_markup=kb.admin_menu())

    # ------------------------------------------------------------------
    @app.on_callback_query(filters.regex(r"^main:"))
    @rate_limited
    async def main_menu_cb(client: Client, cb: CallbackQuery):
        user = cb.from_user
        # SOFT-SHUTDOWN GATE — owner keeps full access
        from handlers.requests import is_bot_offline
        if user and is_bot_offline() and not guard.is_owner(user.id):
            await cb.answer("🛑 Aura is offline. 😴", show_alert=True)
            return
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
                DEV.format(support=config.SUPPORT_CHAT or "—", version=config.BOT_VERSION),
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
            owner = guard.is_owner(user.id)
            if owner:
                await safe_edit(
                    cb.message,
                    WELCOME_OWNER.format(name=config.BOT_NAME, first=user.first_name or "Owner"),
                    kb.main_menu(admin, is_owner=True),
                )
            else:
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
    active = len(await ctx.STREAMER.active_calls()) if ctx.STREAMER else 0
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
        await _with_logo(message.reply_photo, txt, kb.back_to_main())
