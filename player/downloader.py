"""
Media resolution + downloading.

Uses yt-dlp for YouTube (search + URLs) and streams; downloads to a local
cache dir so playback is stable and files can be auto-cleaned.
"""
import asyncio
import os
import time

import yt_dlp
from pyrogram.types import Message

import config
from player.manager import Track

logger = __import__("modules.logger", fromlist=["x"]).setup_logging()


def _sanitize(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in " ._-")[:60].strip()


CLIENT_FALLBACKS = ["tv", "tv_embedded", "web_embedded", "android_vr", "mweb", "web_safari"]


def _ydl_opts(is_video: bool, client: str | None = None) -> dict:
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    fmt = (
        "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
        if is_video
        else "bestaudio/best"
    )
    opts = {
        "format": fmt,
        "outtmpl": os.path.join(config.CACHE_DIR, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "merge_output_format": "mp4" if is_video else "mp3",
        "postprocessors": (
            [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}]
            if not is_video
            else []
        ),
        "max_filesize": config.MAX_TRACK_SIZE_MB * 1024 * 1024,
    }
    # Alternate player clients dodge datacenter bot-checks; cookies fix hard blocks.
    if client and client != "default":
        opts["extractor_args"] = {"youtube": [f"player_client={client}"]}
    if config.COOKIES_FILE:
        opts["cookiefile"] = config.COOKIES_FILE
    return opts


def _is_bot_block(err) -> bool:
    msg = str(err).lower()
    return "sign in to confirm" in msg or "not a bot" in msg or "bot check" in msg


def _run_ydl(query: str, is_video: bool, download: bool, outtmpl: str | None = None) -> dict:
    """
    Extract/download with automatic client rotation.
    Rotates player clients only on YouTube bot-checks; real errors surface directly.
    """
    clients = [config.YT_CLIENT] if config.YT_CLIENT and config.YT_CLIENT != "default" else []
    clients += [c for c in CLIENT_FALLBACKS if c not in clients]

    last_err: Exception | None = None
    for client in clients:
        try:
            opts = _ydl_opts(is_video, client)
            if outtmpl:
                opts["outtmpl"] = outtmpl
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(query, download=download)
                return info
        except Exception as e:  # noqa: BLE001 — rotation logic
            last_err = e
            if not _is_bot_block(e):
                break
            logger.warning("YouTube bot-check hit with client %s — rotating…", client)

    if last_err and _is_bot_block(last_err):
        raise RuntimeError(
            "YouTube is blocking this server's IP (bot check). "
            "Fix: set COOKIES_FILE to a cookies.txt from a logged-in browser "
            "(see README), or run on a VPS with a clean IP."
        )
    raise last_err


def _search_terms(query: str) -> str:
    query = query.strip()
    if query.startswith(("http://", "https://", "www.")):
        return query
    if query.startswith("ytsearch:"):
        return query
    return f"ytsearch1:{query}"


def extract_info(url_or_query: str) -> dict:
    """Fetch metadata for a URL or search query (sync, run in executor)."""
    query = _search_terms(url_or_query)
    info = _run_ydl(query, is_video=False, download=False)
    if "entries" in info:
        info = info["entries"][0]
    return info


async def resolve_track(app, message: Message, query: str, is_video: bool = False) -> Track:
    """
    Resolve a query/url into a downloaded Track.
    Handles: YouTube links, plain searches, and Telegram audio/video files.
    """
    loop = asyncio.get_running_loop()
    requester = message.from_user

    # ----- Telegram file? -----
    media = message.audio or message.video or message.document
    if media is not None and (query in ("", "file")):
        file_path = await app.download_media(message)
        if not file_path:
            raise RuntimeError("Could not download the Telegram file.")
        title = getattr(media, "title", None) or getattr(media, "file_name", "Telegram media")
        duration = getattr(media, "duration", 0) or 0
        if not is_video and message.video is not None:
            is_video = True
        return Track(
            title=title,
            duration=duration,
            file_path=file_path,
            source="telegram",
            requester_id=requester.id if requester else 0,
            requester_name=(requester.first_name or "") if requester else "",
            is_video=is_video,
        )

    # ----- yt-dlp -----
    info = await loop.run_in_executor(None, extract_info, query)
    title = info.get("title") or info.get("id") or "Unknown"
    duration = int(info.get("duration") or 0)
    if config.MAX_DURATION and duration > config.MAX_DURATION:
        raise RuntimeError(f"Track is {duration}s — longer than the {config.MAX_DURATION}s limit.")

    ydl_opts = _ydl_opts(is_video)
    outtmpl = os.path.join(
        config.CACHE_DIR, f"{int(time.time())}_{_sanitize(title)[:30]}.%(ext)s"
    )

    def _download():
        return _run_ydl(query, is_video, download=True, outtmpl=outtmpl)

    await loop.run_in_executor(None, _download)

    files = sorted(
        (f for f in os.listdir(config.CACHE_DIR) if f.startswith(os.path.basename(ydl_opts["outtmpl"]).split("%(ext)s")[0])),
        key=lambda f: os.path.getmtime(os.path.join(config.CACHE_DIR, f)),
        reverse=True,
    )
    if not files:
        raise RuntimeError("Download finished but no file was found in cache.")
    file_path = os.path.join(config.CACHE_DIR, files[0])

    return Track(
        title=title,
        duration=duration,
        file_path=file_path,
        source=query,
        requester_id=requester.id if requester else 0,
        requester_name=(requester.first_name or "") if requester else "",
        is_video=is_video,
        thumbnail=info.get("thumbnail", ""),
        stream_url=info.get("webpage_url", ""),
    )


def cleanup_cache() -> int:
    """Remove cached files older than CACHE_CLEANUP_OLDER_THAN_H hours. Returns bytes freed."""
    if not os.path.isdir(config.CACHE_DIR):
        return 0
    freed = 0
    cutoff = time.time() - config.CACHE_CLEANUP_OLDER_THAN_H * 3600
    for f in os.listdir(config.CACHE_DIR):
        p = os.path.join(config.CACHE_DIR, f)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                freed += os.path.getsize(p)
                os.remove(p)
        except OSError:
            continue
    return freed


def delete_file(path: str) -> None:
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass
