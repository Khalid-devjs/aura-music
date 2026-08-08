"""v1.5.0 end-to-end: play -> external call death -> play again (must recover)."""
import asyncio
import random
import sys
import time

sys.path.insert(0, "/root/musicbot")
import config  # noqa: E402
import pyrogram.errors as _pyro_errors  # noqa: E402
from pyrogram.errors import RPCError as _RPCError  # noqa: E402

for _name in ("GroupcallForbidden", "GroupcallInvalid"):
    if not hasattr(_pyro_errors, _name):
        setattr(
            _pyro_errors,
            _name,
            type(_name, (_RPCError,), {"ID": _name.upper(), "CODE": 400, "VALUE": 0}),
        )

from pyrogram import Client  # noqa: E402
from pyrogram.raw import functions, types  # noqa: E402
from pytgcalls import PyTgCalls
from pytgcalls.types import GroupCallConfig
from pytgcalls.types.stream import MediaStream, AudioQuality, VideoQuality

GROUP = -1003563320323
TEST_VIDEO = "/root/musicbot/downloads/video_2026-08-08_11-05-02_7671599730039717912.mp4"


async def ensure_call(pytgcalls, user, chat_id):
    """Mimic v1.5.0 _ensure_call: create + inject fresh call into cache."""
    if not await _call_active(user, chat_id):
        result = await user.invoke(
            functions.phone.CreateGroupCall(
                peer=await user.resolve_peer(chat_id),
                random_id=random.getrandbits(31),
                rtmp_stream=False,
            )
        )
        fresh = None
        for update in result.updates:
            if isinstance(update, types.UpdateGroupCall) and isinstance(update.call, types.GroupCall):
                fresh = types.InputGroupCall(id=update.call.id, access_hash=update.call.access_hash)
                break
        if fresh is not None:
            pytgcalls._app._bind_client._cache.set_cache(chat_id, fresh)
            print(f"  injected fresh call {fresh.id}")
        await asyncio.sleep(2.5)
    else:
        print("  call already active")


async def _call_active(user, chat_id):
    try:
        peer = await user.resolve_peer(chat_id)
        full = await user.invoke(functions.channels.GetFullChannel(channel=peer))
        call = getattr(full, "call", None)
        if call is None:
            return False
        await user.invoke(functions.phone.GetGroupCall(call=call, limit=0))
        return True
    except Exception:
        return False


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
            break

    pytgcalls = PyTgCalls(user)
    await pytgcalls.start()

    # 1) FIRST PLAY
    print("=== PLAY 1 (fresh) ===")
    await ensure_call(pytgcalls, user, GROUP)
    t0 = time.time()
    await pytgcalls.play(
        GROUP,
        MediaStream(TEST_VIDEO, audio_parameters=AudioQuality.HIGH, video_parameters=VideoQuality.HD_720p),
        GroupCallConfig(auto_start=True),
    )
    print(f"PLAY 1 OK in {time.time()-t0:.1f}s")
    await asyncio.sleep(8)
    print("  (playing 8s)")

    # 2) EXTERNAL CALL DEATH: discard the call as if a random admin closed it
    print("=== EXTERNAL DEATH: DiscardGroupCall ===")
    peer = await user.resolve_peer(GROUP)
    full = await user.invoke(functions.channels.GetFullChannel(channel=peer))
    call = getattr(full, "call", None)
    if call is not None:
        await user.invoke(functions.phone.DiscardGroupCall(call=call))
        print("  discarded, sleeping 3s")
    await asyncio.sleep(3)

    # 3) SECOND PLAY — must recover via cache injection
    print("=== PLAY 2 (after death — v1.5.0 recovery) ===")
    await ensure_call(pytgcalls, user, GROUP)
    t0 = time.time()
    await pytgcalls.play(
        GROUP,
        MediaStream(TEST_VIDEO, audio_parameters=AudioQuality.HIGH, video_parameters=VideoQuality.HD_720p),
        GroupCallConfig(auto_start=True),
    )
    print(f"PLAY 2 OK in {time.time()-t0:.1f}s")
    await asyncio.sleep(5)
    print("  (playing 5s)")

    await pytgcalls.leave_call(GROUP, close=True)
    await user.stop()
    print("=== DONE — both plays succeeded ===")


asyncio.run(main())
