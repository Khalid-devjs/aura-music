"""Verify: does GetGroupCall fail on a discarded call? Does GetFullChannel linger?"""
import asyncio
import random
import sys
import time

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
    async for d in user.get_dialogs():
        if d.chat and d.chat.id == GROUP:
            peer = await user.resolve_peer(GROUP)
            break

    # close any existing
    full = await user.invoke(functions.channels.GetFullChannel(channel=peer))
    old = getattr(full, "call", None)
    if old is not None:
        try:
            await user.invoke(functions.phone.DiscardGroupCall(call=old))
            await asyncio.sleep(2)
        except Exception as e:
            print("discard old err:", type(e).__name__, e)

    # create fresh
    result = await user.invoke(
        functions.phone.CreateGroupCall(
            peer=peer,
            random_id=random.getrandbits(31),
            title="Aura Music",
        )
    )
    fresh = None
    for u in result.updates:
        if isinstance(u, types.UpdateGroupCall) and isinstance(u.call, types.GroupCall):
            fresh = types.InputGroupCall(id=u.call.id, access_hash=u.call.access_hash)
    print("created:", fresh.id if fresh else None)

    # GetGroupCall on a LIVE call
    try:
        gc = await user.invoke(functions.phone.GetGroupCall(call=fresh, limit=0))
        print("GetGroupCall(live): OK — participants:", gc.call.participants_count)
    except Exception as e:
        print("GetGroupCall(live) FAILED:", type(e).__name__, e)

    # discard
    await user.invoke(functions.phone.DiscardGroupCall(call=fresh))
    print("discarded:", fresh.id)

    # GetGroupCall on the DISCARDED call
    try:
        gc = await user.invoke(functions.phone.GetGroupCall(call=fresh, limit=0))
        print("GetGroupCall(discarded): OK?!", gc.call.participants_count)
    except Exception as e:
        print("GetGroupCall(discarded) FAILED:", type(e).__name__, e)

    # GetFullChannel right after discard — does it linger?
    for i in range(1, 6):
        await asyncio.sleep(1)
        full = await user.invoke(functions.channels.GetFullChannel(channel=peer))
        call = getattr(full, "call", None)
        if call is not None:
            print(f"t+{i}s after discard: GetFullChannel STILL reports call {call.id}")
        else:
            print(f"t+{i}s after discard: GetFullChannel clear ✓")
            break
    await user.stop()


asyncio.run(main())
