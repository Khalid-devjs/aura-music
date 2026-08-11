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


CLIENT_FALLBACKS = ["web_safari", "android", "ios", "web", "tv", "mweb", "tv_simply", "web_embedded", "android_vr"]


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
        # PO token (Proof-of-Origin) defeats YouTube's "Sign in to confirm
        # you're not a bot" / "The page needs to be reloaded" blocks from
        # datacenter IPs (2026+). The bgutil HTTP provider (local Deno server
        # on :4416, see /tmp/bgutil-ytdlp-pot-provider) mints tokens via
        # Botguard; the provider PLUGIN is installed in site-packages
        # (yt_dlp_plugins/extractor/getpot_bgutil_http.py) so yt-dlp finds it.
        # Config format: po_token=<client>+<provider> (provider = bgutil:http).
        # Verified 2026-08-11: web_safari + PO token works; COOKIES actually
        # BREAK it from a datacenter IP ("The page needs to be reloaded").
        "extractor_args": {
            "youtube": [
                "player_client=web_safari",
                "po_token=web+bgutil:http",
            ]
        },
    }
    # Alternate player clients dodge datacenter bot-checks; cookies fix hard blocks.
    if client and client != "default" and client != "web" and client != "web_safari":
        # MERGE with the po_token config — replacing would drop the token.
        existing = opts.get("extractor_args", {}).get("youtube", [])
        opts["extractor_args"] = {
            "youtube": [*existing, f"player_client={client}"]
        }
    if config.COOKIES_FILE:
        opts["cookiefile"] = config.COOKIES_FILE
    return opts


def _is_bot_block(err) -> bool:
    msg = str(err).lower()
    return (
        "sign in to confirm" in msg
        or "not a bot" in msg
        or "bot check" in msg
        or "page needs to be reloaded" in msg
        or "requested format is not available" in msg
    )


def _run_ydl(query: str, is_video: bool, download: bool, outtmpl: str | None = None) -> dict:
    """
    Extract/download with automatic client rotation.
    Rotates player clients only on YouTube bot-checks; real errors surface directly.
    Searches repeat the full rotation 3x (bot-checks are probabilistic).
    """
    is_search = query.startswith("ytsearch")
    clients = [config.YT_CLIENT] if config.YT_CLIENT and config.YT_CLIENT != "default" else []
    clients += [c for c in CLIENT_FALLBACKS if c not in clients]

    last_err: Exception | None = None
    # Bot-checks are probabilistic: repeat the WHOLE client rotation a few
    # times. Each rotation = a fresh chance (~17%/attempt → ~80% over 9).
    for rotation in range(3 if is_search else 1):
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
                    raise
                logger.warning(
                    "YouTube bot-check with client %s (rotation %d/3) — rotating…",
                    client, rotation + 1,
                )
        if is_search and last_err and _is_bot_block(last_err):
            time.sleep(2.0)  # brief pause between full rotations

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


_LAST_YT_SEARCH_FALLBACK = 0.0


def _yt_search_fallback(query: str) -> str:
    """Search YouTube via a web search engine when the YouTube search
    endpoint is bot-blocked from this IP. Returns a direct watch URL
    (which yt-dlp can always resolve with the PO token)."""
    global _LAST_YT_SEARCH_FALLBACK
    import urllib.parse
    import urllib.request

    now = time.monotonic()
    if now - _LAST_YT_SEARCH_FALLBACK < 2.0:
        raise RuntimeError("Search throttled — try again in a moment.")
    _LAST_YT_SEARCH_FALLBACK = now

    engines = [
        ("https://search.brave.com/search?q={q}", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"),
        ("https://html.duckduckgo.com/html/?q={q}", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"),
    ]
    last_err: Exception | None = None
    for tmpl, ua in engines:
        try:
            q = urllib.parse.quote(f"site:youtube.com {query}")
            req = urllib.request.Request(
                tmpl.format(q=q),
                headers={"User-Agent": ua},
            )
            html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
            m = re.search(r"youtube\.com/watch\?v=([A-Za-z0-9_-]{11})", html)
            if m:
                return f"https://www.youtube.com/watch?v={m.group(1)}"
        except Exception as e:  # noqa: BLE001 — try next engine
            last_err = e
            continue
    raise RuntimeError(f"No YouTube results found for that search. ({last_err})")


def extract_info(url_or_query: str) -> dict:
    """Fetch metadata for a URL or search query (sync, run in executor).
    Retries a few times (YouTube bot-checks are probabilistic per request),
    then falls back to web-engine search → direct URL."""
    query = _search_terms(url_or_query)
    is_search = query.startswith("ytsearch")

    attempts = 3 if is_search else 1
    last_err: Exception | None = None
    for attempt in range(attempts):
        try:
            info = _run_ydl(query, is_video=False, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return info
        except Exception as e:  # noqa: BLE001 — retry logic
            last_err = e
            if not _is_bot_block(e):
                raise
            logger.warning("YouTube search bot-check (attempt %d/%d): %s", attempt + 1, attempts, str(e)[:60])
            time.sleep(1.0 * (attempt + 1))  # backoff between retries

    # all retries bot-blocked → try web-engine search → direct watch URL
    direct = _yt_search_fallback(url_or_query)
    logger.warning("Search endpoint blocked — web-engine fallback → %s", direct)
    info = _run_ydl(direct, is_video=False, download=False)
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

    # Download the RESOLVED URL (not the raw search query) — if extract_info
    # fell back to DDG search, the direct watch URL is the only thing that
    # downloads reliably from this IP.
    dl_target = info.get("webpage_url") or query

    def _download():
        return _run_ydl(dl_target, is_video, download=True, outtmpl=outtmpl)

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
