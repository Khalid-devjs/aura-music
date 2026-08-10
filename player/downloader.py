"""
Media resolution + downloading.

Uses yt-dlp for YouTube (search + URLs) and streams; downloads to a local
cache dir so playback is stable and files can be auto-cleaned.
"""
import asyncio
import os
import re
import time

import logging

import yt_dlp
from pyrogram.types import Message

import config
from player.manager import Track

logger = logging.getLogger("auramusic")


def _sanitize(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in " ._-")[:60].strip()


CLIENT_FALLBACKS = ["web", "tv", "android", "ios", "mweb"]


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
        "merge_output_format": "mp4" if is_video else "m4a",
        # NO post-processing for audio: the original m4a/opus/webm plays
        # directly and skips the ffmpeg re-encode step (big startup speedup).
        # ffmpeg/ffprobe are only used by yt-dlp for merging/thumbnail, which
        # is cheap.
        "postprocessors": (
            []
            if not is_video
            else [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}]
        ),
        "max_filesize": config.MAX_TRACK_SIZE_MB * 1024 * 1024,
        # deno JS runtime + remote EJS challenge-solver script are REQUIRED for
        # YouTube signature/n solving (2026+). Without them most formats come
        # back without URLs -> "Requested format is not available".
        "remote_components": {"ejs:github"},
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
    if re.match(r"^ytsearch\d*:", query):  # already a ytsearch URL (idempotent)
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
    requester = message.from_user

    # ----- Telegram file? -----
    media = message.audio or message.video or message.document
    if media is not None and (query in ("", "file")):
        file_path = await app.download_media(message)
        if not file_path:
            raise RuntimeError("Could not download the Telegram file.")
        title = getattr(media, "title", None) or getattr(media, "file_name", None) or "Telegram media"
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
            file_id=getattr(media, "file_id", ""),
        )

    # ----- yt-dlp (YouTube link / search / direct media URL) -----
    return await _resolve_ytdlp(query, is_video, requester)


async def resolve_url(
    url: str, is_video: bool = False, requester_id: int = 0, requester_name: str = ""
) -> Track:
    """Replay helper: resolve a saved URL back into a downloadable Track (no Message needed)."""
    return await _resolve_ytdlp(url, is_video, None, requester_id, requester_name)


async def _resolve_ytdlp(
    query: str,
    is_video: bool,
    requester,
    requester_id: int = 0,
    requester_name: str = "",
) -> Track:
    """Shared yt-dlp path used by both resolve_track (new plays) and resolve_url (replays)."""
    query = _search_terms(query)  # wrap plain searches so BOTH extract & download work
    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, extract_info, query)
    title = info.get("title") or info.get("id") or "Unknown"
    duration = int(info.get("duration") or 0)
    if config.MAX_DURATION and duration > config.MAX_DURATION:
        raise RuntimeError(f"Track is {duration}s — longer than the {config.MAX_DURATION}s limit.")

    outtmpl = os.path.join(
        config.CACHE_DIR, f"{int(time.time())}_{_sanitize(title)[:30]}.%(ext)s"
    )

    def _download():
        return _run_ydl(query, is_video, download=True, outtmpl=outtmpl)

    await loop.run_in_executor(None, _download)

    # Pick the FINISHED media file. yt-dlp leaves `.part` fragments behind
    # when a download stalls or is interrupted — those are truncated and
    # would make the stream die mid-track (and the call auto-close). Only
    # complete files (mp4/m4a/webm/opus/mp3) qualify.
    files = sorted(
        (
            f
            for f in os.listdir(config.CACHE_DIR)
            if f.startswith(os.path.basename(outtmpl).split("%(ext)s")[0])
            and not f.endswith(".part")
            and not f.endswith(".ytdl")
        ),
        key=lambda f: os.path.getmtime(os.path.join(config.CACHE_DIR, f)),
        reverse=True,
    )
    if not files:
        # a stale .part exists but no finished file → the download failed
        raise RuntimeError(
            "Download did not complete (only a partial file exists). "
            "The source may be throttled — try again."
        )
    file_path = os.path.join(config.CACHE_DIR, files[0])

    rid = requester.id if requester else requester_id
    rname = (requester.first_name or "") if requester else requester_name
    return Track(
        title=title,
        duration=duration,
        file_path=file_path,
        source=query,
        requester_id=rid,
        requester_name=rname,
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
