"""Test the REAL py-tgcalls play() path with a fresh call — captures the exact failure."""
import asyncio
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
from pytgcalls import PyTgCalls  # noqa: E402
from pytgcalls.types import GroupCallConfig  # noqa: E402
from pytgcalls.types.stream import MediaStream, AudioQuality, VideoQuality  # noqa: E402

GROUP = -1003563320323
FILE = "/tmp/aura_test_audio.mp3"


async def main() -> None:
    # minimal audio file for the test
    import subprocess
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=5", "-q:a", "9", FILE],
        capture_output=True,
    )

    user = Client(
        "diag_user_tmp",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        session_string=config.SESSION_STRING,
        workers=8,
    )
    await user.start()
    async for d in user.get_dialogs():
        if d.chat and d.chat.id == GROUP:
            await user.resolve_peer(GROUP)
            break

    app = PyTgCalls(user)
    await app.start()

    # close any lingering call
    try:
        await app.leave_call(GROUP, close=True)
    except Exception as e:
        print("leave:", type(e).__name__, e)

    print(f"-- attempt at t0 --")
    t0 = time.monotonic()
    try:
        await app.play(
            GROUP,
            MediaStream(
                FILE,
                audio_parameters=AudioQuality.HIGH,
                video_parameters=VideoQuality.SD_360p,
            ),
            GroupCallConfig(auto_start=True),
        )
        print(f"PLAY OK in {time.monotonic()-t0:.1f}s")
        await asyncio.sleep(8)
    except Exception as e:
        print(f"PLAY FAILED in {time.monotonic()-t0:.1f}s: {type(e).__name__}: {e}")

    # inspect internal state
    cache = app._app._cache
    try:
        print("full_chat_cache:", cache._full_chat_cache._cache if hasattr(cache._full_chat_cache, "_cache") else cache._full_chat_cache)
    except Exception as e:
        print("cache inspect err:", e)

    await asyncio.sleep(2)
    try:
        await app.leave_call(GROUP, close=True)
    except Exception:
        pass
    await app.stop()
    await user.stop()


asyncio.run(main())
