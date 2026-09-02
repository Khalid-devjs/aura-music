"""
Lightweight controller to reload the active USER client / Streamer at runtime.

Owner commands:
  /reloadstreamer   reload user client from latest saved session
  /streamerlist     list saved streamer accounts
  /usestreamer <phone>  use a specific saved account
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message

from handlers import context as ctx
from modules import filters as guard
from modules.helpers import send_or_edit
from modules.ratelimit import rate_limited

SESSION_ROOT = Path.home() / ".auramusic_sessions"
LATEST_FILE = SESSION_ROOT / "latest.session"


def register(app) -> None:
    @app.on_message(filters.command("reloadstreamer"), group=3)
    @rate_limited
    async def _reload(client: Client, message: Message):
        user = message.from_user
        if not guard.is_owner(user.id):
            await send_or_edit(message, "🔒 Owner only.")
            return
        text = await _load_latest()
        if not text:
            await send_or_edit(message, "ℹ️ No saved streamer sessions found. Use `/createsession`.")
            return
        await ctx.reload_streamer(text)
        await send_or_edit(message, "✅ Streamer reloaded from latest session.")

    @app.on_message(filters.command("streamerlist"), group=3)
    @rate_limited
    async def _list(client: Client, message: Message):
        user = message.from_user
        if not guard.is_owner(user.id):
            await send_or_edit(message, "🔒 Owner only.")
            return
        files = sorted(SESSION_ROOT.glob("*.session"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            await send_or_edit(message, "No streamer accounts saved.")
            return
        lines = ["📋 **Streamer accounts**"]
        for p in files[:20]:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))
            lines.append(f"• `{p.name}` — {ts}")
        await send_or_edit(message, "\n".join(lines))

    @app.on_message(filters.command("usestreamer"), group=3)
    @rate_limited
    async def _use(client: Client, message: Message):
        user = message.from_user
        if not guard.is_owner(user.id):
            await send_or_edit(message, "🔒 Owner only.")
            return
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) < 2:
            await send_or_edit(message, "Usage: `/usestreamer <phone>`\nExample: `/usestreamer 2348012345678`")
            return
        phone = parts[1].strip().lstrip("+")
        path = SESSION_ROOT / f"{phone}.session"
        if not path.exists():
            await send_or_edit(message, f"❌ No session found for `{phone}`.")
            return
        text = path.read_text(encoding="utf-8")
        await ctx.reload_streamer(text)
        await send_or_edit(message, f"✅ Streamer switched to `{path.name}`.")


async def _load_latest() -> str | None:
    if LATEST_FILE.exists():
        try:
            return LATEST_FILE.read_text(encoding="utf-8")
        except Exception:
            return None
    files = sorted(SESSION_ROOT.glob("*.session"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    try:
        return files[0].read_text(encoding="utf-8")
    except Exception:
        return None
