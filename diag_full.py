"""Compare GetFullChannel's call field: NYASH (healthy) vs CYBER SPACE (broken)."""
import asyncio
import random
import sys

sys.path.insert(0, "/root/musicbot")
import config  # noqa: E402
from pyrogram import Client  # noqa: E402
from pyrogram.raw import functions  # noqa: E402

GROUPS = {-1003700155577: "NYASH", -1003563320323: "CYBER SPACE"}


async def dump_call(user, label, cid) -> None:
    try:
        peer = await user.resolve_peer(cid)
    except Exception as e:
        print(f"[{label}] resolve FAIL: {e}")
        return
    full = await user.invoke(functions.channels.GetFullChannel(channel=peer))
    chatfull = full.full_chat
    call = getattr(chatfull, "call", None)
    print(f"[{label}] ChatFull.call = {call}")
    # also inspect flags/attrs that might hide the call
    attrs = {}
    for a in dir(chatfull):
        if a.startswith("_") or callable(getattr(chatfull, a)):
            continue
        v = getattr(chatfull, a)
        if v is not None and v is not False:
            attrs[a] = v if not isinstance(v, bytes) else f"<{len(v)}B>"
    print(f"[{label}] ChatFull attrs: {attrs}")
    if call is not None:
        gc = await user.invoke(functions.phone.GetGroupCall(call=call, limit=0))
        print(f"[{label}]   live call id={gc.call.id} participants={gc.call.participants_count} schedule={gc.call.schedule_date}")


async def main() -> None:
    user = Client(
        "diag_user_tmp",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        session_string=config.SESSION_STRING,
        workers=4,
    )
    await user.start()
    # warm peer cache: fresh clients can't resolve these out-of-range IDs directly
    async for d in user.get_dialogs():
        if d.chat and d.chat.id in GROUPS:
            print(f"[warm] {GROUPS[d.chat.id]}: {d.chat.title!r} id={d.chat.id}")
    for cid, label in GROUPS.items():
        await dump_call(user, label, cid)
    await user.stop()


asyncio.run(main())
