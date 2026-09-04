"""
Owner-only streamer account creation with explicit pending state.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid
from pyrogram.types import Message

import config
from handlers import context as ctx
from modules import filters as guard
from modules.helpers import send_or_edit
from modules.ratelimit import rate_limited

SESSION_ROOT = Path.home() / ".auramusic_sessions"
SESSION_ROOT.mkdir(parents=True, exist_ok=True)


def _session_path(phone: str) -> Path:
    safe = phone.replace("+", "").replace(" ", "")
    return SESSION_ROOT / f"{safe}.session"


def _write_session(path: Path, session_string: str) -> None:
    path.write_text(session_string, encoding="utf-8")


_STEP = {
    "API_ID": "Send your **API_ID** (numeric).",
    "API_HASH": "Send your **API_HASH**.",
    "PHONE": "Send the **phone number** in international format, e.g. `+2348012345678`.",
    "CODE": "Send the **login code** you received on Telegram.",
    "2FA": "🔐 Two-step verification is enabled. Send your **password**.",
}


def register(app: Client) -> None:
    @app.on_message(filters.command("createsession"), group=3)
    @rate_limited
    async def _start(client: Client, message: Message):
        user = message.from_user
        print(f"[CREATESESSION] cmd by user={user.id if user else 'None'}, owner={config.OWNER_ID}")
        if not user or not guard.is_owner(user.id):
            print("[CREATESESSION] reject: not owner")
            await send_or_edit(message, "🔒 Owner only.")
            return
        if not config.API_ID or not config.API_HASH or not config.BOT_TOKEN:
            await send_or_edit(message, "❌ Bot API credentials missing in config.")
            return
        ctx.pending.set(
            user.id,
            "createsession",
            data={"step": "API_ID", "started_at": int(time.time())},
        )
        print("[CREATESESSION] pending set, sending prompt")
        await send_or_edit(message, "🧾 **Create Streamer Session**\n\n" + _STEP["API_ID"])

    @app.on_message(filters.private, group=4)
    async def _step(client: Client, message: Message):
        user = message.from_user
        text = message.text.strip() if message.text else ""
        print(f"[CREATESESSION] private msg from={user.id if user else 'None'}, text={text[:40]}")
        if not user or not text:
            print("[CREATESESSION] skip: no user/text")
            return
        if not guard.is_owner(user.id):
            print(f"[CREATESESSION] skip: not owner {user.id}")
            return
        req = ctx.pending.pop(user.id)
        if not req or req.get("action") != "createsession":
            print(f"[CREATESESSION] skip: no pending createsession (action={req.get('action') if req else 'None'})")
            return
        data = req.get("data") or {}
        step = data.get("step")
        print(f"[CREATESESSION] processing step={step}")
        try:
            if step == "API_ID":
                api_id = int(text)
                data["api_id"] = api_id
                data["step"] = "API_HASH"
                await send_or_edit(message, "Step 2:\n" + _STEP["API_HASH"])
            elif step == "API_HASH":
                data["api_hash"] = text
                data["step"] = "PHONE"
                await send_or_edit(message, "Step 3:\n" + _STEP["PHONE"])
            elif step == "PHONE":
                phone = text.lstrip("@")
                data["phone"] = phone
                await send_or_edit(message, "📲 Sending login code to Telegram…")
                try:
                    tmp_name = f"tmp_{int(time.time()*1000)}"
                    tmp = Client(tmp_name, api_id=data["api_id"], api_hash=data["api_hash"], phone_number=phone)
                    await tmp.connect()
                    sent = await tmp.send_code(phone)
                    await tmp.disconnect()
                except Exception as e:
                    await send_or_edit(message, f"❌ Failed to send code: `{e}`")
                    return
                data["phone_code_hash"] = sent.phone_code_hash
                data["step"] = "CODE"
                await send_or_edit(message, "Step 4:\n" + _STEP["CODE"])
            elif step == "CODE":
                phone = data["phone"]
                path = _session_path(phone)
                try:
                    tmp_name = f"tmp_{int(time.time()*1000)}"
                    tmp = Client(tmp_name, api_id=data["api_id"], api_hash=data["api_hash"], phone_number=phone)
                    await tmp.connect()
                    signed_in = await tmp.sign_in(phone, data["phone_code_hash"], text)
                    if isinstance(signed_in, SessionPasswordNeeded):
                        data["step"] = "2FA"
                        data["path"] = str(path)
                        ctx.pending.set(user.id, "createsession", data=data)
                        await send_or_edit(message, _STEP["2FA"])
                        return
                    session_string = await tmp.export_session_string()
                    await tmp.disconnect()
                    await _finalize(client, message, path, session_string, phone)
                    return
                except PhoneCodeInvalid:
                    await send_or_edit(message, "❌ Invalid code. Try `/createsession` again.")
                    return
                except Exception as e:
                    await send_or_edit(message, f"❌ Login failed: `{e}`")
                    return
            elif step == "2FA":
                phone = data["phone"]
                path = Path(data.get("path") or _session_path(phone))
                try:
                    tmp_name = f"tmp_{int(time.time()*1000)}"
                    tmp = Client(tmp_name, api_id=data["api_id"], api_hash=data["api_hash"], phone_number=phone)
                    await tmp.connect()
                    await tmp.check_password(text)
                    session_string = await tmp.export_session_string()
                    await tmp.disconnect()
                    await _finalize(client, message, path, session_string, phone)
                    return
                except Exception as e:
                    await send_or_edit(message, f"❌ 2FA failed: `{e}`")
                    return
            else:
                await send_or_edit(message, "❌ Unknown step. Try `/createsession` again.")
                return
        except Exception as e:
            print(f"[CREATESESSION] ERROR: {e}")
            await send_or_edit(message, f"❌ Error: `{e}`")
            return
        print(f"[CREATESESSION] saving pending, next_step={data.get('step')}")
        ctx.pending.set(user.id, "createsession", data=data)


async def _finalize(client: Client, message: Message, path: Path, session_string: str, phone: str):
    try:
        await asyncio.to_thread(_write_session, path, session_string)
        await send_or_edit(
            message,
            "✅ Streamer account created.\n"
            f"Phone: `{phone}`\n"
            f"Saved: `{path}`\n\n"
            "Restarting Streamer client…",
        )
        try:
            await ctx.reload_streamer(session_string)
            await send_or_edit(message, "✅ Streamer client reloaded with the new session.")
        except Exception as e:
            await send_or_edit(message, f"⚠️ Saved, but auto-reload failed: `{e}`\nUse `/reloadstreamer`.")
    except Exception as e:
        await send_or_edit(message, f"❌ Failed to save session: `{e}`")
