"""
Owner-only account factory: create a new Telegram USER account session from
inline credentials and persist it.

Commands:
  /createsession   -> ask for API_ID
  /setapihash     -> ask for API_HASH
  /setphone       -> ask for phone number
  /setcode        -> ask for Telegram login code
  /set2fa         -> optional 2FA password if enabled
"""
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


def register(app) -> None:
    @app.on_message(filters.command("createsession"), group=3)
    @rate_limited
    async def _start(client: Client, message: Message):
        user = message.from_user
        if not guard.is_owner(user.id):
            await send_or_edit(message, "🔒 Owner only.")
            return
        ctx.pending.set(
            user.id,
            "createsession_api_id",
            data={"step": "api_id", "started_at": int(time.time())},
        )
        await send_or_edit(message, "🧾 **Create Streamer Session**\n\nStep 1: Send your **API_ID** (numeric).")

    @app.on_message(filters.private, group=4)
    async def _step(client: Client, message: Message):
        user = message.from_user
        if not guard.is_owner(user.id) or not message.text:
            return
        req = ctx.pending.pop(user.id)
        if not req:
            return
        action = req.get("action")
        data = req.get("data") or {}
        text = message.text.strip()

        if action != "createsession_api_id":
            return
        try:
            api_id = int(text)
        except ValueError:
            await send_or_edit(message, "❌ API_ID must be numeric. Try `/createsession` again.")
            return
        ctx.pending.set(
            user.id,
            "createsession_api_hash",
            data={"api_id": api_id, "step": "api_hash", "started_at": int(time.time())},
        )
        await send_or_edit(message, "Step 2: Send your **API_HASH**.")

        @app.on_message(filters.private, group=4)
        async def _api_hash(client: Client, message: Message):
            user2 = message.from_user
            if not guard.is_owner(user2.id) or not message.text:
                return
            req2 = ctx.pending.pop(user2.id)
            if not req2 or req2.get("action") != "createsession_api_hash":
                return
            api_id = req2["data"]["api_id"]
            api_hash = message.text.strip()
            ctx.pending.set(
                user2.id,
                "createsession_phone",
                data={"api_id": api_id, "api_hash": api_hash, "step": "phone", "started_at": int(time.time())},
            )
            await send_or_edit(message, "Step 3: Send the **phone number** in international format, e.g. `+2348012345678`.")

            @app.on_message(filters.private, group=4)
            async def _phone(client: Client, message: Message):
                user3 = message.from_user
                if not guard.is_owner(user3.id) or not message.text:
                    return
                req3 = ctx.pending.pop(user3.id)
                if not req3 or req3.get("action") != "createsession_phone":
                    return
                phone = message.text.strip().lstrip("@")
                api_id = req3["data"]["api_id"]
                api_hash = req3["data"]["api_hash"]
                path = _session_path(phone)
                await send_or_edit(message, "📲 Sending login code to Telegram…")
                try:
                    tmp_name = f"tmp_{int(time.time()*1000)}"
                    tmp = Client(tmp_name, api_id=api_id, api_hash=api_hash, phone_number=phone)
                    await tmp.connect()
                    sent = await tmp.send_code(phone)
                    await tmp.disconnect()
                except Exception as e:
                    await send_or_edit(message, f"❌ Failed to send code: `{e}`")
                    return
                ctx.pending.set(
                    user3.id,
                    "createsession_code",
                    data={
                        "api_id": api_id,
                        "api_hash": api_hash,
                        "phone": phone,
                        "path": str(path),
                        "phone_code_hash": sent.phone_code_hash,
                        "step": "code",
                        "started_at": int(time.time()),
                    },
                )
                await send_or_edit(message, "Step 4: Send the **login code** you received on Telegram.")

                @app.on_message(filters.private, group=4)
                async def _code(client: Client, message: Message):
                    user4 = message.from_user
                    if not guard.is_owner(user4.id) or not message.text:
                        return
                    req4 = ctx.pending.pop(user4.id)
                    if not req4 or req4.get("action") != "createsession_code":
                        return
                    code = message.text.strip()
                    api_id = req4["data"]["api_id"]
                    api_hash = req4["data"]["api_hash"]
                    phone = req4["data"]["phone"]
                    path = Path(req4["data"]["path"])
                    phone_code_hash = req4["data"]["phone_code_hash"]
                    try:
                        tmp_name = f"tmp_{int(time.time()*1000)}"
                        tmp = Client(tmp_name, api_id=api_id, api_hash=api_hash, phone_number=phone)
                        await tmp.connect()
                        signed_in = await tmp.sign_in(phone, phone_code_hash, code)
                        if isinstance(signed_in, SessionPasswordNeeded):
                            await send_or_edit(message, "🔐 Two-step verification is enabled. Send your **password**.")
                            ctx.pending.set(
                                user4.id,
                                "createsession_2fa",
                                data={
                                    "api_id": api_id,
                                    "api_hash": api_hash,
                                    "phone": phone,
                                    "path": str(path),
                                    "step": "2fa",
                                    "started_at": int(time.time()),
                                },
                            )

                            @app.on_message(filters.private, group=4)
                            async def _2fa(client: Client, message: Message):
                                user5 = message.from_user
                                if not guard.is_owner(user5.id) or not message.text:
                                    return
                                req5 = ctx.pending.pop(user5.id)
                                if not req5 or req5.get("action") != "createsession_2fa":
                                    return
                                password = message.text.strip()
                                api_id = req5["data"]["api_id"]
                                api_hash = req5["data"]["api_hash"]
                                phone = req5["data"]["phone"]
                                path = Path(req5["data"]["path"])
                                try:
                                    tmp_name = f"tmp_{int(time.time()*1000)}"
                                    tmp = Client(tmp_name, api_id=api_id, api_hash=api_hash, phone_number=phone)
                                    await tmp.connect()
                                    await tmp.check_password(password)
                                    session_string = await tmp.export_session_string()
                                    await tmp.disconnect()
                                except Exception as e:
                                    await send_or_edit(message, f"❌ 2FA failed: `{e}`")
                                    return
                                await _finalize(message, path, session_string, phone)
                            return
                        session_string = await tmp.export_session_string()
                        await tmp.disconnect()
                        await _finalize(message, path, session_string, phone)
                    except PhoneCodeInvalid:
                        await send_or_edit(message, "❌ Invalid code. Try `/createsession` again.")
                    except Exception as e:
                        await send_or_edit(message, f"❌ Login failed: `{e}`")


async def _finalize(message: Message, path: Path, session_string: str, phone: str):
    try:
        await asyncio.to_thread(_write_session, path, session_string)
        await send_or_edit(
            message,
            "✅ Streamer account created.\n"
            f"Phone: `{phone}`\n"
            f"Saved: `{path}`\n\n"
            "Restarting Streamer client…",
        )
    except Exception as e:
        await send_or_edit(message, f"❌ Failed to save session: `{e}`")
