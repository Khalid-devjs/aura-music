"""
Owner-only helper to log in additional streaming Telegram accounts.

Usage:
  /addstreamer
  Bot will ask for:
    1. API_ID
    2. API_HASH
    3. Phone number
    4. Login code (sent to Telegram)
    5. Optional 2FA password

Result: session saved to sessions/{phone}.session
"""
import asyncio
import os
import time

from pyrogram import Client, filters
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid
from pyrogram.types import Message

import config
from database.db import Database
from handlers import context as ctx
from modules import filters as guard
from modules.helpers import send_or_edit
from modules.ratelimit import rate_limited


SESSION_DIR = os.path.join(os.path.dirname(__file__), "..", "sessions")
os.makedirs(SESSION_DIR, exist_ok=True)


def register(app) -> None:
    @app.on_message(filters.command("addstreamer"), group=3)
    @rate_limited
    async def addstreamer_cmd(client: Client, message: Message):
        user = message.from_user
        if not guard.is_owner(user.id):
            await send_or_edit(message, "🔒 Owner only.")
            return

        db: Database = ctx.DB
        await send_or_edit(message, "🎧 **Add Streamer Account**\n\nStep 1: Send your **API_ID** (numeric).")
        ctx.pending.set(
            user.id,
            "addstreamer_api_id",
            data={"step": "api_id", "started_at": int(time.time())},
        )

    @app.on_message(filters.private, group=4)
    async def addstreamer_input(client: Client, message: Message):
        user = message.from_user
        if not guard.is_owner(user.id) or not message.text:
            return
        req = ctx.pending.pop(user.id)
        if not req:
            return
        action = req.get("action")
        data = req.get("data") or {}
        text = message.text.strip()

        if action != "addstreamer_api_id":
            return
        try:
            api_id = int(text)
        except ValueError:
            await send_or_edit(message, "❌ API_ID must be numeric. Try again:")
            ctx.pending.set(
                user.id,
                "addstreamer_api_id",
                data={"step": "api_id", "started_at": int(time.time())},
            )
            return

        await send_or_edit(message, "Step 2: Send your **API_HASH**.")
        ctx.pending.set(
            user.id,
            "addstreamer_api_hash",
            data={"api_id": api_id, "step": "api_hash", "started_at": int(time.time())},
        )

        @app.on_message(filters.private, group=4)
        async def _api_hash_step(client: Client, message: Message):
            user2 = message.from_user
            if not guard.is_owner(user2.id) or not message.text:
                return
            req2 = ctx.pending.pop(user2.id)
            if not req2 or req2.get("action") != "addstreamer_api_hash":
                return
            api_id = req2["data"]["api_id"]
            api_hash = message.text.strip()
            await send_or_edit(message, "Step 3: Send the **phone number** in international format, e.g. `+2348012345678`.")
            ctx.pending.set(
                user2.id,
                "addstreamer_phone",
                data={"api_id": api_id, "api_hash": api_hash, "step": "phone", "started_at": int(time.time())},
            )

            @app.on_message(filters.private, group=4)
            async def _phone_step(client: Client, message: Message):
                user3 = message.from_user
                if not guard.is_owner(user3.id) or not message.text:
                    return
                req3 = ctx.pending.pop(user3.id)
                if not req3 or req3.get("action") != "addstreamer_phone":
                    return
                phone = message.text.strip().lstrip("@")
                api_id = req3["data"]["api_id"]
                api_hash = req3["data"]["api_hash"]
                session_name = os.path.join(SESSION_DIR, phone.replace("+", ""))
                await send_or_edit(message, "📲 Sending login code to Telegram…")
                try:
                    temp = Client(
                        f"streamer_{int(time.time())}",
                        api_id=api_id,
                        api_hash=api_hash,
                        phone_number=phone,
                    )
                    await temp.connect()
                    sent = await temp.send_code(phone)
                except Exception as e:
                    await send_or_edit(message, f"❌ Failed to send code: `{e}`")
                    return

                await send_or_edit(message, "Step 4: Send the **login code** you received on Telegram.")
                ctx.pending.set(
                    user3.id,
                    "addstreamer_code",
                    data={
                        "api_id": api_id,
                        "api_hash": api_hash,
                        "phone": phone,
                        "session_name": session_name,
                        "phone_code_hash": sent.phone_code_hash,
                        "step": "code",
                        "started_at": int(time.time()),
                    },
                )

                @app.on_message(filters.private, group=4)
                async def _code_step(client: Client, message: Message):
                    user4 = message.from_user
                    if not guard.is_owner(user4.id) or not message.text:
                        return
                    req4 = ctx.pending.pop(user4.id)
                    if not req4 or req4.get("action") != "addstreamer_code":
                        return
                    code = message.text.strip()
                    api_id = req4["data"]["api_id"]
                    api_hash = req4["data"]["api_hash"]
                    phone = req4["data"]["phone"]
                    session_name = req4["data"]["session_name"]
                    phone_code_hash = req4["data"]["phone_code_hash"]
                    try:
                        temp = Client(
                            f"streamer_{int(time.time())}",
                            api_id=api_id,
                            api_hash=api_hash,
                            phone_number=phone,
                        )
                        await temp.connect()
                        signed_in = await temp.sign_in(phone, phone_code_hash, code)
                        if isinstance(signed_in, SessionPasswordNeeded):
                            await send_or_edit(message, "🔐 Two-step verification is enabled. Send your **password**.")
                            ctx.pending.set(
                                user4.id,
                                "addstreamer_2fa",
                                data={
                                    "api_id": api_id,
                                    "api_hash": api_hash,
                                    "phone": phone,
                                    "session_name": session_name,
                                    "step": "2fa",
                                    "started_at": int(time.time()),
                                },
                            )

                            @app.on_message(filters.private, group=4)
                            async def _2fa_step(client: Client, message: Message):
                                user5 = message.from_user
                                if not guard.is_owner(user5.id) or not message.text:
                                    return
                                req5 = ctx.pending.pop(user5.id)
                                if not req5 or req5.get("action") != "addstreamer_2fa":
                                    return
                                password = message.text.strip()
                                api_id = req5["data"]["api_id"]
                                api_hash = req5["data"]["api_hash"]
                                phone = req5["data"]["phone"]
                                session_name = req5["data"]["session_name"]
                                try:
                                    temp = Client(
                                        f"streamer_{int(time.time())}",
                                        api_id=api_id,
                                        api_hash=api_hash,
                                        phone_number=phone,
                                    )
                                    await temp.connect()
                                    await temp.check_password(password)
                                    await temp.save_session(session_name + ".session")
                                    await temp.disconnect()
                                    await send_or_edit(
                                        message,
                                        f"✅ Streamer account saved: `{session_name}.session`\n\n"
                                        f"Use `/streamerlist` to see all accounts.",
                                    )
                                    return
                                except Exception as e:
                                    await send_or_edit(message, f"❌ 2FA failed: `{e}`")
                                    return

                            return
                        await temp.save_session(session_name + ".session")
                        await temp.disconnect()
                        await send_or_edit(
                            message,
                            f"✅ Streamer account saved: `{session_name}.session`\n\n"
                            f"Use `/streamerlist` to see all accounts.",
                        )
                    except PhoneCodeInvalid:
                        await send_or_edit(message, "❌ Invalid code. Try `/addstreamer` again.")
                    except Exception as e:
                        await send_or_edit(message, f"❌ Login failed: `{e}`")
