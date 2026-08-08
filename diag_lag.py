"""Measure GetFullChannel propagation lag around call creation."""
import asyncio
import random
import sys
import time

sys.path.insert(0, "/root/musicbot")
import config  # noqa: E402
from pyrogram import Client  # noqa: E402
from pyrogram.raw import functions  # noqa: E402

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

    # close any existing call first (from previous diag)
    full = await user.invoke(functions.channels.GetFullChannel(channel=peer))
    old = getattr(full, "call", None)
    if old is not None:
        print("closing pre-existing call:", old.id)
        try:
            await user.invoke(functions.phone.DiscardGroupCall(call=old))
            await asyncio.sleep(3)
        except Exception as e:
            print("discard err:", e)

    print("-- creating fresh call --")
    t0 = time.monotonic()
    result = await user.invoke(
        functions.phone.CreateGroupCall(
            peer=peer,
            random_id=random.getrandbits(31),
            title="Aura Music",
        )
    )
    t_create = time.monotonic() - t0
    print(f"CreateGroupCall took {t_create:.2f}s")
    new_call = None
    for u in result.updates:
        if isinstance(u, type(result.updates[0])) and "GroupCall" in type(u).__name__:
            print("update:", type(u).__name__, getattr(u, "call", None))
            c = getattr(u, "call", None)
            if c is not None and hasattr(c, "id"):
                new_call = c
    if new_call is not None:
        print("new call from updates:", new_call.id, new_call.access_hash)

    # poll GetFullChannel until the call appears
    for i in range(1, 11):
        await asyncio.sleep(1)
        full = await user.invoke(functions.channels.GetFullChannel(channel=peer))
        call = getattr(full, "call", None)
        if call is not None:
            print(f"t+{i}s: GetFullChannel sees call {call.id}  (created id {getattr(new_call,'id',None)})")
            print(f"  match: {call.id == getattr(new_call, 'id', None)}")
            break
        print(f"t+{i}s: still None")
    else:
        print("!! NEVER became visible in 10s !!")

    # clean up: close the call we created
    full = await user.invoke(functions.channels.GetFullChannel(channel=peer))
    call = getattr(full, "call", None)
    if call is not None:
        try:
            await user.invoke(functions.phone.DiscardGroupCall(call=call))
            print("cleanup: closed call")
        except Exception as e:
            print("cleanup err:", e)

    await user.stop()


asyncio.run(main())
