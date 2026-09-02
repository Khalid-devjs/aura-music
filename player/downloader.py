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
from yt_dlp.plugins import load_all_plugins
from pyrogram.types import Message

import config
from player.manager import Track

logger = logging.getLogger("auramusic")

# CRITICAL: ensure yt-dlp's external plugins (bgutil PO-token provider) are
# loaded. yt-dlp lazy-loads plugins on first YoutubeDL() use, but the bot
# imports THIS module before any YoutubeDL exists — and the PO-token provider
# must be registered before the YouTube extractor builds its token director.
# Without this, the plugin registry stays empty and NO PO tokens are minted.
load_all_plugins()


def _sanitize(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in " .-_")[:60].strip()


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
        "postprocessors": (
            []
            if not is_video
            else [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}]
        ),
        "max_filesize": config.MAX_TRACK_SIZE_MB * 1024 * 1024,
        # PO token (Proof-of-Origin) defeats YouTube's bot-checks from
        # datacenter IPs. The bgutil HTTP provider mints tokens via Botguard.
        #
        # CRITICAL:
        #  - load_all_plugins() at module import ensures provider registry is ready
        #  - po_token CLIENT must match player_client exactly
        #  - BOTH contexts required: web.gvs (search) + web.player (streaming)
        "extractor_args": {
            "youtube": [
                "player_client=web",
                "po_token=web.gvs+bgutil:http",
                "po_token=web.player+bgutil:http",
            ]
        },
        # Be gentle on YouTube to reduce 429/403 blocks
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 1,
        "limit_rate": "5M",
    }
    # Alternate player clients dodge datacenter bot-checks; cookies fix hard blocks.
    if client and client not in {"default", "web", "web_safari"}:
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
        or "http error 403" in msg
        or "429" in msg
    )


def _is_unavailable(err: Exception) -> bool:
    msg = str(err).lower()
    return (
        "video unavailable" in msg
        or "this video is not available" in msg
        or "unavailable" in msg and "format" not in msg
        or "not available in your country" in msg
        or "isn't available" in msg
    )


def _run_ydl(query: str, is_video: bool, download: bool, outtmpl: str | None = None) -> dict:
    """
    Extract/download with automatic client rotation.
    Rotates player clients only on YouTube bot-checks; real errors surface directly.
    """
    is_search = query.startswith("ytsearch")
    clients = [config.YT_CLIENT] if config.YT_CLIENT and config.YT_CLIENT != "default" else []
    clients += [c for c in CLIENT_FALLBACKS if c not in clients]

    last_err: Exception | None = None
    # More conservative retry pattern to avoid hammering YouTube
    rotations = 2 if is_search else 1
    for rotation in range(rotations):
        for client in clients:
            try:
                opts = _ydl_opts(is_video, client)
                if outtmpl:
                    opts["outtmpl"] = outtmpl
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(query, download=download)
                    return info
            except Exception as e:
                last_err = e
                if not _is_bot_block(e):
                    raise
                logger.warning(
                    "YouTube bot-check with client %s (rotation %d/%d) — rotating…",
                    client, rotation + 1, rotations,
                )
        if is_search and last_err and _is_bot_block(last_err):
            time.sleep(3.0)  # back off before retrying

    if last_err and _is_bot_block(last_err):
        raise RuntimeError(
            "YouTube is blocking this server's IP (bot check). "
            "Fix: set COOKIES_FILE to a cookies.txt from a logged-in browser, "
            "or switch to a VPS/residential IP."
        )
    raise last_err


def _search_terms(query: str) -> str:
    query = query.strip()
    if query.startswith(("http://", "https://", "www.")):
        return query
    if re.match(r"^ytsearch\d*:", query):
        return query
    return f"ytsearch1:{query}"


def _yt_search_fallback(query: str) -> str:
    """Search YouTube via public Invidious instances when YouTube search is blocked."""
    from player import search_provider
    results = search_provider.search_youtube(query, limit=3)
    return results[0]["url"]


def _yt_search_results(query: str, limit: int = 8) -> list[dict]:
    """Return search results with titles/durations via Invidious fallback."""
    from player import search_provider
    return search_provider.search_youtube(query, limit=limit)


def extract_info(url_or_query: str) -> dict:
    """
    Fetch metadata for a URL or search query.
    Strategy:
      - Plain search  → Invidious API first, then resolve watch URL via yt-dlp
      - Direct URL    → yt-dlp with PO tokens + client rotation
    """
    query = _search_terms(url_or_query)
    is_search = query.startswith("ytsearch")

    if is_search:
        try:
            results = _yt_search_results(url_or_query, limit=8)
        except Exception as se:
            logger.warning("Invidious search failed: %s", str(se)[:60])
            results = []
        last_err: Exception | None = None
        for r in results:
            direct = r["url"]
            try:
                info = _run_ydl(direct, is_video=False, download=False)
                if "entries" in info:
                    info = info["entries"][0]
                return info
            except Exception as e:
                last_err = e
                if not (_is_bot_block(e) or _is_unavailable(e)):
                    raise
                logger.warning(
                    "Invidious result %s blocked/unavailable — trying next…",
                    direct[-11:],
                )
                continue
        raise RuntimeError(
            "YouTube blocked every search result from this IP. "
            "Try a different track, or set COOKIES_FILE to a cookies.txt from a "
            "logged-in browser."
        ) from last_err

    info = _run_ydl(query, is_video=False, download=False)
    if "entries" in info:
        info = info["entries"][0]
    return info


async def resolve_track(app, message: Message, query: str, is_video: bool = False) -> Track:
    """
    Resolve a query/url into a downloaded Track.
    """
    requester = message.from_user

    # Telegram file?
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

    return await _resolve_ytdlp(query, is_video, requester)


async def resolve_url(
    url: str,
    is_video: bool = False,
    requester_id: int = 0,
    requester_name: str = "",
) -> Track:
    """Replay helper: resolve a saved URL back into a downloadable Track."""
    return await _resolve_ytdlp(url, is_video, None, requester_id, requester_name)


async def _resolve_ytdlp(
    query: str,
    is_video: bool,
    requester,
    requester_id: int = 0,
    requester_name: str = "",
) -> Track:
    """Shared yt-dlp path for both resolve_track and resolve_url."""
    query = _search_terms(query)
    is_search = query.startswith("ytsearch")
    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, extract_info, query)
    title = info.get("title") or info.get("id") or "Unknown"
    duration = int(info.get("duration") or 0)
    if config.MAX_DURATION and duration > config.MAX_DURATION:
        raise RuntimeError(f"Track is {duration}s — longer than the {config.MAX_DURATION}s limit.")

    outtmpl = os.path.join(
        config.CACHE_DIR, f"{int(time.time())}_{_sanitize(title)[:30]}.%(ext)s"
    )

    candidates = [info.get("webpage_url") or query]
    if is_search:
        raw_query = re.sub(r"^ytsearch\d*:", "", query)
        try:
            candidates += [r["url"] for r in _yt_search_results(raw_query, limit=8)]
        except Exception:
            pass

    seen: set[str] = set()
    candidates = [u for u in candidates if not (u in seen or seen.add(u))]

    downloaded: dict | None = None
    last_err: Exception | None = None

    for i, dl_target in enumerate(candidates):
        def _download():
            return _run_ydl(dl_target, is_video, download=True, outtmpl=outtmpl)

        try:
            downloaded = await loop.run_in_executor(None, _download)
            break
        except Exception as e:
            last_err = e
            if not (_is_bot_block(e) or _is_unavailable(e)):
                raise
            logger.warning(
                "Candidate %s failed (%s) — trying next…",
                dl_target[-30:],
                str(e)[:40],
            )
            for f in os.listdir(config.CACHE_DIR):
                base = os.path.basename(outtmpl).split("%(ext)s")[0]
                if f.startswith(base) and f.endswith(".part"):
                    try:
                        os.remove(os.path.join(config.CACHE_DIR, f))
                    except OSError:
                        pass

    if downloaded is None:
        if last_err is None:
            last_err = RuntimeError("Download failed: no candidates were tried.")
        if _is_bot_block(last_err):
            raise RuntimeError(
                "Download blocked by YouTube bot-check. "
                "Set COOKIES_FILE to a cookies.txt from a logged-in browser."
            ) from last_err
        raise last_err

    if downloaded and isinstance(downloaded, dict) and downloaded.get("_vm"):
        vm_dest = locals().get("dest", "")
        if not vm_dest or not os.path.exists(vm_dest):
            raise RuntimeError("Downloaded file missing after transfer.")
        rid = requester.id if requester else requester_id
        rname = (requester.first_name or "") if requester else requester_name
        return Track(
            title=title,
            duration=duration,
            file_path=vm_dest,
            source=query,
            requester_id=rid,
            requester_name=rname,
            is_video=is_video,
            thumbnail=info.get("thumbnail", ""),
            stream_url=info.get("webpage_url", ""),
        )

    if isinstance(downloaded, dict):
        title = downloaded.get("title") or title
        duration = int(downloaded.get("duration") or duration)
        info = downloaded

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
        raise RuntimeError(
            "Download did not complete (only a partial file exists). "
            "The source may be throttled — try again."
        )
    file_path = os.path.join(config.CACHE_DIR, files[0])
    return Track(
        title=title,
        duration=duration,
        file_path=file_path,
        source=query,
        requester_id=requester.id if requester else requester_id,
        requester_name=(requester.first_name or "") if requester else requester_name,
        is_video=is_video,
        thumbnail=info.get("thumbnail", ""),
        stream_url=info.get("webpage_url", ""),
    )


def cleanup_cache() -> int:
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
