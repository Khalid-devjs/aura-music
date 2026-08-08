"""Ground-truth diagnostic: live call state + membership for the problem group."""
import asyncio
import sys

sys.path.insert(0, "/root/musicbot")
import config  # noqa: E402
from pyrogram import Client  # noqa: E402
from pyrogram.raw import functions  # noqa: E402

GROUP = -1003563320323  # problem group


async def check(client, name: str) -> None:
    try:
        peer = await client.resolve_peer(GROUP)
        print(f"[{name}] resolve_peer OK -> {type(peer).__name__}")
    except Exception as e:
        print(f"[{name}] resolve_peer FAIL: {type(e).__name__}: {e}")
        return

    try:
        full = await client.invoke(
            functions.channels.GetFullChannel(channel=peer)
        )
        call = getattr(full, "call", None)
        print(f"[{name}] GetFullChannel: call = {call}")
        if call is not None:
            gc = await client.invoke(functions.phone.GetGroupCall(call=call, limit=0))
            c = gc.call
            print(
                f"[{name}]   call.id={c.id} schedule_date={c.schedule_date} "
                f"participants_count={c.participants_count} title={c.title!r}"
            )
            parts = await client.invoke(
                functions.phone.GetGroupParticipants(
                    call=call, ids=[], sources=[], offset="", limit=50
                )
            )
            for p in parts.participants[:8]:
                uid = getattr(getattr(p, "peer", None), "user_id", None)
                print(f"[{name}]   participant: {uid} (muted={p.muted}, joined={p.date})")
    except Exception as e:
        print(f"[{name}] GetFullChannel FAIL: {type(e).__name__}: {e}")

    # membership check
    try:
        member = await client.get_chat_member(GROUP, 8957935298)  # userbot
        print(f"[{name}] userbot membership: {member.status}")
    except Exception as e:
        print(f"[{name}] membership check FAIL: {type(e).__name__}: {e}")


async def main() -> None:
    bot = Client(
        "diag_bot_tmp",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        workers=4,
    )
    user = Client(
        "auramusic_user",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        session_string=config.SESSION_STRING,
        workers=4,
    )
    await bot.start()
    await user.start()
    print("=== both clients started ===")
    await check(bot, "BOT")
    await check(user, "USERBOT")
    await bot.stop()
    await user.stop()


asyncio.run(main())
