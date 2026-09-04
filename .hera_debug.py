from pyrogram import Client, filters
from pyrogram.types import Message

def register(app: Client):
    @app.on_message(filters.command("ping") & filters.private, group=9)
    async def _ping(client, message: Message):
        try:
            await message.reply("pong")
        except Exception as e:
            print("DM DEBUG ping error:", e)

    @app.on_message(filters.private, group=9)
    async def _any_dm(client, message: Message):
        try:
            text = (message.text or message.caption or "[non-text]").strip()[:120]
            print(f"DM DEBUG from {message.from_user.id if message.from_user else '?'}: {text}")
        except Exception:
            pass
