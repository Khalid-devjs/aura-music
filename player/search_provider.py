"""
YouTube search provider fallback.

When YouTube's search endpoint is bot-blocked from the server IP (the
"Sign in to confirm you're not a bot" wall), this module searches through
public Invidious instances instead. Invidious scrapes YouTube from ITS OWN
servers and returns JSON to us, so our IP never contacts YouTube for the
search — while direct video downloads still go through yt-dlp + PO tokens
from our IP (which works).

Instances are tried in order; the first that returns results wins. A
failed instance is remembered so we don't keep hammering it.
"""
import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("auramusic")

# Public Invidious API instances (https://api.invidious.io). Order is a
# rough liveness guess; failures rotate the working instance.
INSTANCES = [
    "https://invidious.materialio.us",
    "https://invidious.f5.si",
    "https://invidious.nerdvpn.de",
    "https://inv.nadeko.net",
    "https://invidious.privacyredirect.com",
    "https://yewtu.be",
    "https://invidious.jing.rocks",
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# Instance -> last failure timestamp (monotonic); failed instances are
# skipped for a cooldown window so we don't waste time on dead ones.
_failures: dict[str, float] = {}
COOLDOWN = 300.0  # 5 min

# Cache of last successful instance so subsequent searches go straight to it.
_working: str | None = None


def _is_video_url(url: str) -> bool:
    return (
        "youtube.com/watch" in url
        or "youtu.be/" in url
        or "music.youtube.com/watch" in url
    )


def _mark_failed(instance: str) -> None:
    _failures[instance] = time.monotonic()


def _candidate_instances() -> list[str]:
    now = time.monotonic()
    fresh = [i for i in INSTANCES if now - _failures.get(i, 0.0) > COOLDOWN]
    if _working and _working in fresh:
        # Prefer the last known-good instance, but shuffle the rest so we
        # spread load and discover live ones.
        return [_working] + [i for i in fresh if i != _working]
    return fresh


def search_youtube(query: str, limit: int = 8) -> list[dict]:
    """Search YouTube through a public Invidious instance.

    Returns a list of dicts:
        {videoId, title, duration, author, url}
    Raises RuntimeError if every instance fails.
    """
    global _working
    q = urllib.parse.quote(query)
    last_err: Exception | None = None

    for instance in _candidate_instances():
        url = f"{instance}/api/v1/search?q={q}&type=video"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                payload = json.loads(r.read().decode("utf-8", "ignore"))
            if not isinstance(payload, list) or not payload:
                raise RuntimeError("empty result set")

            results = []
            for item in payload[:limit]:
                vid = item.get("videoId")
                if not vid:
                    continue
                results.append(
                    {
                        "videoId": vid,
                        "title": item.get("title") or "Unknown",
                        "duration": int(item.get("lengthSeconds") or 0),
                        "author": item.get("author") or "",
                        "url": f"https://www.youtube.com/watch?v={vid}",
                    }
                )
            if not results:
                raise RuntimeError("no valid video entries")
            _working = instance
            logger.info("Invidious search via %s: %d results", instance, len(results))
            return results
        except Exception as e:  # noqa: BLE001 — try next instance
            last_err = e
            _mark_failed(instance)
            logger.warning("Invidious instance %s failed: %s", instance, str(e)[:70])
            continue

    raise RuntimeError(
        f"Search provider unavailable: all Invidious instances failed. ({last_err})"
    )
