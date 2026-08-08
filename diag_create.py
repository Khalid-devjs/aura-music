"""Manual raw test: CreateGroupCall -> GetFullChannel -> JoinGroupCall."""
import asyncio
import random
import sys

sys.path.insert(0, "/root/musicbot")
import config  # noqa: E402
from pyrogram import Client  # noqa: E402
from pyrogram.raw import functions, types  # noqa: E402

GROUP = -1003563320323


async def main() -> None:
    user = Client(
        "diag_user_tmp",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        session_string=config.SESSION_STRING,
        workers=4,
    )
    await user.start()
    print("userbot started")

    # warm the peer cache like the bot does: try resolving via a known path
    try:
        peer = await user.resolve_peer(GROUP)
        print("resolve_peer OK:", type(peer).__name__)
    except Exception as e:
        print("resolve_peer FAIL:", e)
        # fallback: find via dialogs
        print("-- trying dialogs to warm peer cache --")
        async for d in user.get_dialogs():
            if d.chat and d.chat.id == GROUP:
                print("found dialog:", d.chat.title, d.chat.id)
                peer = await user.resolve_peer(GROUP)
                print("resolve now OK:", type(peer).__name__)
                break
        else:
            print("group NOT in userbot dialogs!")
            return

    full = await user.invoke(functions.channels.GetFullChannel(channel=peer))
    print("call before create:", getattr(full, "call", None))

    # create a fresh call
    print("-- CreateGroupCall --")
    result = await user.invoke(
        functions.phone.CreateGroupCall(
            peer=peer,
            random_id=random.getrandbits(31),
            title="Aura Music",
        )
    )
    for u in result.updates:
        if isinstance(u, types.UpdateGroupCall):
            print("UpdateGroupCall:", type(u.call).__name__, "| schedule:", getattr(u.call, "schedule_date", None))

    await asyncio.sleep(4)

    full2 = await user.invoke(functions.channels.GetFullChannel(channel=peer))
    call2 = getattr(full2, "call", None)
    print("call after create+4s:", call2)
    if call2 is not None:
        gc = await user.invoke(functions.phone.GetGroupCall(call=call2, limit=0))
        print("  call.id:", gc.call.id, "| schedule:", gc.call.schedule_date, "| participants:", gc.call.participants_count)
    else:
        print("  !! GetFullChannel still reports NO call after CreateGroupCall !!")

    await user.stop()


asyncio.run(main())
