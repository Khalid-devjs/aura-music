"""Test: join immediately using the InputGroupCall from CreateGroupCall's response."""
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
    async for d in user.get_dialogs():
        if d.chat and d.chat.id == GROUP:
            peer = await user.resolve_peer(GROUP)
            break

    # close pre-existing call
    full = await user.invoke(functions.channels.GetFullChannel(channel=peer))
    old = getattr(full, "call", None)
    if old is not None:
        try:
            await user.invoke(functions.phone.DiscardGroupCall(call=old))
            print("closed old call", old.id)
            await asyncio.sleep(2)
        except Exception as e:
            print("discard err:", e)

    # create fresh call and capture its InputGroupCall from the response
    result = await user.invoke(
        functions.phone.CreateGroupCall(
            peer=peer,
            random_id=random.getrandbits(31),
            title="Aura Music",
        )
    )
    new_call = None
    for u in result.updates:
        if isinstance(u, types.UpdateGroupCall) and isinstance(u.call, types.GroupCall):
            new_call = types.InputGroupCall(id=u.call.id, access_hash=u.call.access_hash)
            print("fresh call from response:", u.call.id)
    if new_call is None:
        print("!! no UpdateGroupCall in response")
        await user.stop()
        return

    # immediately try JoinGroupCall with the fresh ID (no GetFullChannel)
    try:
        join = await user.invoke(
            functions.phone.JoinGroupCall(
                call=new_call,
                params=types.DataJSON(data="{\"ufrag\":\"\",\"pwd\":\"\",\"fingerprints\":[],\"ssrc\":0}"),
                muted=False,
                join_as=await user.resolve_peer("me"),
                video_stopped=True,
                invite_hash="",
            )
        )
        print("JOIN IMMEDIATELY: OK")
        for u in join.updates:
            if isinstance(u, types.UpdateGroupCallConnection):
                print("  transport:", u.params.data[:80])
    except Exception as e:
        print(f"JOIN IMMEDIATELY FAILED: {type(e).__name__}: {e}")

    # now wait and retry join with GetFullChannel path
    for i in range(1, 6):
        await asyncio.sleep(2)
        full = await user.invoke(functions.channels.GetFullChannel(channel=peer))
        call = getattr(full, "call", None)
        if call is not None:
            print(f"t+{i*2}s: GetFullChannel sees call, trying join...")
            try:
                join = await user.invoke(
                    functions.phone.JoinGroupCall(
                        call=call,
                        params=types.DataJSON(data="{\"ufrag\":\"\",\"pwd\":\"\",\"fingerprints\":[],\"ssrc\":0}"),
                        muted=False,
                        join_as=await user.resolve_peer("me"),
                        video_stopped=True,
                        invite_hash="",
                    )
                )
                print(f"  JOIN via GetFullChannel OK at t+{i*2}s")
                break
            except Exception as e:
                print(f"  join err: {type(e).__name__}: {e}")
        else:
            print(f"t+{i*2}s: GetFullChannel still None")

    # cleanup
    try:
        full = await user.invoke(functions.channels.GetFullChannel(channel=peer))
        call = getattr(full, "call", None)
        if call is not None:
            await user.invoke(functions.phone.DiscardGroupCall(call=call))
            print("cleanup done")
    except Exception as e:
        print("cleanup err:", e)
    await user.stop()


asyncio.run(main())
