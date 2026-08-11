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
# Without this, the plugin registry stays empty and NO PO tokens are minted
# (verified: provider list empty when downloader imported first).
load_all_plugins()


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
        # CRITICAL (verified 2026-08-11):
        #  - load_all_plugins() at module import (yt-dlp lazy-loads plugins,
        #    bot's import order left the provider registry EMPTY otherwise)
        #  - po_token CLIENT must match player_client exactly (yt-dlp skips
        #    mismatched tokens)
        #  - BOTH contexts required: web.gvs (search) + web.player (video
        #    data/streaming). Missing .player → "HTTP Error 403: Forbidden"
        #    on download.
        "extractor_args": {
            "youtube": [
                "player_client=web",
                "po_token=web.gvs+bgutil:http",
                "po_token=web.player+bgutil:http",
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
        or "http error 403" in msg  # video-level flag: 403 on stream fetch
    )


def _is_unavailable(err: Exception) -> bool:
    """Videos that YouTube reports as unavailable/unplayable — retryable by
    trying a different search candidate, not a real error."""
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
    Searches repeat the full rotation 3x (bot-checks are probabilistic).
    """
    is_search = query.startswith("ytsearch")
    clients = [config.YT_CLIENT] if config.YT_CLIENT and config.YT_CLIENT != "default" else []
    clients += [c for c in CLIENT_FALLBACKS if c not in clients]

    last_err: Exception | None = None
    # Bot-checks are probabilistic: repeat the WHOLE client rotation a few
    # times. Each rotation = a fresh chance (~17%/attempt → ~80% over 9).
    # Direct URL downloads don't triple-rotate: the candidate retry loop in
    # _resolve_ytdlp already tries other videos, so one rotation is enough.
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


def _yt_search_fallback(query: str) -> str:
    """Search YouTube via public Invidious instances when the YouTube search
    endpoint is bot-blocked from this IP. Returns a direct watch URL
    (which yt-dlp can always resolve with the PO token)."""
    from player import search_provider

    results = search_provider.search_youtube(query, limit=3)
    return results[0]["url"]


def _yt_search_results(query: str, limit: int = 8) -> list[dict]:
    """Return search results with titles/durations via Invidious fallback."""
    from player import search_provider

    return search_provider.search_youtube(query, limit=limit)


def extract_info(url_or_query: str) -> dict:
    """Fetch metadata for a URL or search query (sync, run in executor).

    Strategy (2026-08: YouTube bot-blocks this server's IP for SEARCH):
      - Plain search  → Invidious API first (IP-free, ~2s), then resolve the
        returned watch URL via yt-dlp. This avoids the slow yt-dlp search
        rotations that burn 60s+ and then fail anyway.
      - Direct URL    → yt-dlp with PO tokens + client rotation (works for
        most videos; some are flagged even on clean IPs).
    """
    query = _search_terms(url_or_query)
    is_search = query.startswith("ytsearch")

    if is_search:
        # 1) Invidious search (fast, IP-free)
        try:
            results = _yt_search_results(url_or_query, limit=8)
        except Exception as se:  # noqa: BLE001
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
            except Exception as e:  # noqa: BLE001 — try next result
                last_err = e
                if not (_is_bot_block(e) or _is_unavailable(e)):
                    raise
                logger.warning("Invidious result %s blocked/unavailable — trying next…", direct[-11:])
                continue

        # 2) All Invidious results blocked → Kernel cloud VM search (clean IP)
        try:
            from player import kernel_downloader

            if kernel_downloader._is_configured():
                vm_results = kernel_downloader.search_youtube_vm(url_or_query, limit=8)
                for r in vm_results:
                    direct = r["url"]
                    try:
                        info = _run_ydl(direct, is_video=False, download=False)
                        if "entries" in info:
                            info = info["entries"][0]
                        return info
                    except Exception as e:  # noqa: BLE001 — try next result
                        last_err = e
                        if not (_is_bot_block(e) or _is_unavailable(e)):
                            raise
                        logger.warning("VM result %s blocked — trying next…", direct[-11:])
                        continue
        except Exception as ve:  # noqa: BLE001 — VM is best-effort
            logger.warning("Kernel VM search failed: %s", str(ve)[:80])

        raise RuntimeError(
            "YouTube blocked every search result from this IP. "
            "Try a different track, or set COOKIES_FILE to a cookies.txt from a "
            "logged-in browser (see README)."
        ) from last_err

    # Direct URL: yt-dlp with PO tokens + client rotation
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

    # Download the RESOLVED URL (not the raw search query). If the video
    # itself is flagged (some videos bot-block even on a clean IP), fall
    # back to more Invidious search results and try each in turn.
    candidates = [info.get("webpage_url") or query]
    if is_search:
        raw_query = re.sub(r"^ytsearch\d*:", "", query)
        try:
            candidates += [r["url"] for r in _yt_search_results(raw_query, limit=8)]
        except Exception:  # noqa: BLE001 — search fallback is best-effort
            pass
    # de-dupe while keeping order
    seen: set[str] = set()
    candidates = [u for u in candidates if not (u in seen or seen.add(u))]

    downloaded: dict | None = None
    last_err: Exception | None = None

    # If the kernel VM fallback is configured and the server IP is known to
    # be flagged, skip straight to it after ONE quick local attempt.
    kernel_ready = False
    try:
        from player import kernel_downloader

        kernel_ready = kernel_downloader._is_configured()
    except Exception:  # noqa: BLE001
        kernel_ready = False

    for i, dl_target in enumerate(candidates):
        def _download():
            return _run_ydl(dl_target, is_video, download=True, outtmpl=outtmpl)

        try:
            downloaded = await loop.run_in_executor(None, _download)
            break
        except Exception as e:  # noqa: BLE001 — try next candidate
            last_err = e
            if not (_is_bot_block(e) or _is_unavailable(e)):
                raise
            logger.warning("Candidate %s failed (%s) — trying next…", dl_target[-30:], str(e)[:40])
            # clean partial files before retrying with the next candidate
            for f in os.listdir(config.CACHE_DIR):
                if f.startswith(os.path.basename(outtmpl).split("%(ext)s")[0]) and f.endswith(".part"):
                    try:
                        os.remove(os.path.join(config.CACHE_DIR, f))
                    except OSError:
                        pass
            # Fast path: if the FIRST local candidate bot-blocked and the
            # kernel VM is configured, skip the remaining local candidates
            # (they will almost certainly fail too) and go straight to the VM.
            if i == 0 and kernel_ready and _is_bot_block(last_err):
                logger.info("Local download bot-blocked — switching to Kernel VM…")
                break
    if downloaded is None:
        # All local candidates failed with bot-block/unavailable → try the
        # Kernel cloud VM (clean IP) if it's configured.
        if last_err is None:
            last_err = RuntimeError("Download failed: no candidates were tried.")
        if _is_bot_block(last_err):
            dest = ""
            try:
                from player import kernel_downloader

                vm_path = await loop.run_in_executor(
                    None,
                    kernel_downloader.download_via_vm,
                    candidates[0],
                )
                if vm_path and os.path.exists(vm_path):
                    # move into the cache dir so the normal file-scan below works
                    dest = os.path.join(
                        config.CACHE_DIR,
                        f"{int(time.time())}_{_sanitize(title)[:30]}.mp3",
                    )
                    if dest != vm_path:
                        try:
                            os.replace(vm_path, dest)
                        except OSError:
                            os.rename(vm_path, dest)
                            dest = vm_path
                    file_path = dest
                    # scan finds it below; use the already-known title/duration
                    files = [os.path.basename(file_path)]
                    downloaded = {"_vm": True}
                    logger.info("Kernel VM download succeeded → %s", file_path)
            except Exception as ke:  # noqa: BLE001 — kernel is best-effort
                logger.warning("Kernel VM download failed: %s", str(ke)[:80])
                raise last_err from ke
        if downloaded is None:
            raise last_err

    if downloaded and isinstance(downloaded, dict) and downloaded.get("_vm"):
        # Kernel VM fallback: file already placed at `dest`.
        vm_dest = locals().get("dest", "")
        if not vm_dest or not os.path.exists(vm_dest):
            raise RuntimeError("Kernel VM download: file missing after transfer.")
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

    # If a fallback candidate downloaded (not the first pick), use ITS
    # metadata so the title/duration/thumbnail shown match the audio.
    if isinstance(downloaded, dict):
        title = downloaded.get("title") or title
        duration = int(downloaded.get("duration") or duration)
        info = downloaded

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
