"""
Kernel cloud downloader (clean-IP fallback).

When YouTube bot-blocks this server's IP, this module routes downloads
through a Kernel cloud browser VM (https://kernel.sh) whose datacenter IP
is NOT flagged. The VM runs yt-dlp (no PO tokens needed — clean IP),
converts to mp3, then POSTs the file back through the SSH reverse tunnel
(-R 9090:127.0.0.1:9090) to the local file-drop server, where the bot
picks it up.

Setup (one-time, see README):
  1. kernel browsers create --headless  (with remote_forward 4416+9090)
  2. kernel browsers ssh <session> -R 4416:127.0.0.1:4416 -R 9090:127.0.0.1:9090
  3. curl -L -o /tmp/yt-dlp https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp
  4. chmod +x /tmp/yt-dlp

The VM control happens through Kernel's MCP (mcp_kernel_* tools), which
this module shells out to via the Kernel HTTP API so the bot doesn't need
the MCP server loaded at runtime.

This module is best-effort: if the VM/tunnel is down, callers fall back
to direct yt-dlp on this server.
"""
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

import config

logger = logging.getLogger("auramusic")

# Kernel MCP endpoint + auth (same key the MCP server uses).
_MCP_URL = "https://mcp.onkernel.com/mcp"
_API_KEY = os.getenv("MCP_KERNEL_API_KEY", "").strip()

# Browser session running yt-dlp (set by deploy/ops; read from env).
KERNEL_SESSION_ID = os.getenv("KERNEL_SESSION_ID", "").strip()

# File-drop server auth token (must match KERNEL_DROP_TOKEN on the bot side).
_DROP_TOKEN = config.KERNEL_DROP_TOKEN

_DROP_DIR = config.KERNEL_DROP_DIR
os.makedirs(_DROP_DIR, exist_ok=True)

# Unique marker so we don't pick up files from other requests.
_job_counter = 0


def _rpc(method: str, params: dict) -> dict:
    """Call the Kernel MCP endpoint (JSON-RPC over HTTP)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        _MCP_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode("utf-8", "ignore")
    # SSE frames: "event: message\ndata: {json}"
    data = None
    for line in raw.splitlines():
        if line.startswith("data: "):
            data = json.loads(line[6:])
            break
    if data is None:
        data = json.loads(raw)
    if "error" in data and data["error"]:
        raise RuntimeError(f"Kernel RPC {method} failed: {data['error']}")
    return data.get("result") or {}


def _exec_vm(args: list[str], timeout: int = 90) -> str:
    """Run a command in the Kernel VM; returns stdout."""
    if not _API_KEY or not KERNEL_SESSION_ID:
        raise RuntimeError("Kernel downloader not configured (KERNEL_SESSION_ID / MCP_KERNEL_API_KEY).")
    # Kernel's exec_command caps timeout_sec at 150 — clamp to avoid RPC rejection.
    timeout = min(int(timeout), 140)
    result = _rpc("tools/call", {
        "name": "exec_command",
        "arguments": {
            "session_id": KERNEL_SESSION_ID,
            "command": args[0],
            "args": args[1:],
            "timeout_sec": timeout,
        },
    })
    # result shape: {"content":[{"type":"text","text":"{...}"}]}
    text = ""
    for item in (result.get("content") or []):
        if item.get("type") == "text":
            text += item.get("text", "")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "stdout" in parsed:
            return parsed.get("stdout", "")
    except (ValueError, TypeError):
        pass
    return text


def _is_configured() -> bool:
    return bool(_API_KEY and KERNEL_SESSION_ID and _DROP_TOKEN)


def _quote(v: str) -> str:
    return '"%s"' % v.replace('"', '\\"')


def search_youtube_vm(query: str, limit: int = 8) -> list[dict]:
    """Search YouTube from the clean VM IP using yt-dlp."""
    out = _exec_vm([
        "/tmp/yt-dlp", "--flat-playlist", "--dump-single-json", "--no-warnings",
        "--quiet", f"ytsearch{limit}:{query}",
    ], timeout=60)
    try:
        data = json.loads(out)
    except ValueError:
        logger.warning("Kernel search: could not parse yt-dlp output")
        raise RuntimeError("Kernel search returned no usable data.")
    results = []
    for e in (data.get("entries") or []):
        vid = e.get("id")
        if not vid:
            continue
        results.append({
            "videoId": vid,
            "title": e.get("title") or "Unknown",
            "duration": int(e.get("duration") or 0),
            "author": e.get("channel") or e.get("uploader") or "",
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
        if len(results) >= limit:
            break
    return results


def download_via_vm(url: str, is_video: bool = False, out_prefix: str = "kernel") -> str:
    """Download via the Kernel VM and return the local file path.

    is_video=True  → mp4 video (best ≤720p), no conversion
    is_video=False → mp3 audio (bestaudio converted)
    Returns the path of the received file. Raises RuntimeError on failure.
    """
    global _job_counter
    if not _is_configured():
        raise RuntimeError("Kernel downloader not configured.")
    _job_counter += 1
    job = f"k{int(time.time())}_{_job_counter}"
    outtmpl = f"/tmp/{job}.%(ext)s"

    if is_video:
        # best mp4 video (video+audio merged by yt-dlp), cap at 720p
        cmd = [
            "/tmp/yt-dlp", "-f",
            "bestvideo[height<=720]+bestaudio/best[height<=720]/best[height<=720]",
            "--merge-output-format", "mp4",
            "-o", outtmpl, "--no-warnings", "--quiet", url,
        ]
    else:
        # audio: download + convert to mp3
        cmd = [
            "/tmp/yt-dlp", "-f", "bestaudio/best", "-x", "--audio-format", "mp3",
            "-o", outtmpl, "--no-warnings", "--quiet", url,
        ]

    # 1) download (+ convert) in the VM — retry a couple times (VM/yt-dlp can stall)
    produced = False
    fname = None
    for attempt in range(1, 4):
        try:
            _exec_vm(cmd, timeout=140)
        except Exception as e:  # RPC/exec hiccup — retry
            logger.warning("Kernel download attempt %d failed: %s", attempt, e)
        listing = _exec_vm(["ls", "-1", "/tmp"], timeout=20)
        wanted_ext = "mp3" if not is_video else "mp4"
        fname = None
        for f in listing.splitlines():
            if f.startswith(job) and f.endswith(f".{wanted_ext}"):
                fname = f
                break
        if not fname and is_video:
            for f in listing.splitlines():
                if f.startswith(job) and f.endswith((".mp4", ".mkv", ".webm")):
                    fname = f
                    break
        if fname:
            produced = True
            break
        time.sleep(2)

    if not produced:
        raise RuntimeError(f"Kernel VM download produced no {wanted_ext} for {url}")

    # 3) POST it through the tunnel to the local file-drop server
    drop_name = f"{job}.{fname.rsplit('.', 1)[-1]}"
    _exec_vm([
        "curl", "-s", "-X", "POST",
        "-H", f"X-Drop-Token: {_DROP_TOKEN}",
        "-H", f"X-File-Name: {drop_name}",
        "--data-binary", f"@/tmp/{fname}",
        "http://localhost:9090/upload",
    ], timeout=60)

    # 4) wait for the file to land locally
    dest = os.path.join(_DROP_DIR, drop_name)
    deadline = time.time() + 15
    while time.time() < deadline:
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            # move into the cache dir so normal cleanup picks it up
            _ext = os.path.splitext(dest)[1] or (".mp4" if is_video else ".mp3")
            final = os.path.join(config.CACHE_DIR, f"{int(time.time())}_{job}{_ext}")
            try:
                os.replace(dest, final)
            except OSError:
                final = dest
            return final
        time.sleep(0.5)
    raise RuntimeError("Kernel download: file did not arrive in drop dir.")
