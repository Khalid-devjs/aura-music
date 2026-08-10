"""
PyTgCalls streaming wrapper.

Bound to the USER-account Pyrogram client (voice chats are user-only).
Manages: join/play/pause/resume/stop/volume, auto-advance on StreamEnded,
re-join on unexpected disconnect, file cleanup after each track.
"""
import asyncio
import logging
import random
import time

from pyrogram import Client
from pyrogram.raw.functions.phone import CreateGroupCall
from pyrogram.raw.types import GroupCall, InputGroupCall, UpdateGroupCall
from pytgcalls import PyTgCalls
from pytgcalls.types import ChatUpdate, GroupCallConfig, MediaStream, StreamEnded, Update
from pytgcalls.types.stream import AudioQuality, VideoQuality

import config
from player import downloader
from player.manager import Track, manager

logger = logging.getLogger("auramusic")


class Streamer:
    def __init__(self, app: Client):
        self.app = app
        self.pytgcalls = PyTgCalls(app)
        self._lock = asyncio.Lock()
        self._playing_file = {}  # chat_id -> file path (for cleanup)
        self._created_calls = {}  # chat_id -> (monotonic_time, InputGroupCall|None)

    # ------------------------------------------------------------------
    async def start(self) -> None:
        # NOTE: on_update() is a decorator FACTORY in py-tgcalls 2.2.x —
        # must be called: on_update()(handler). `on_update(handler)` silently
        # registers nothing (the decorator is returned, never applied).
        self.pytgcalls.on_update()(self._on_update)
        await self.pytgcalls.start()
        logger.info("PyTgCalls started (update handler registered)")

    # ------------------------------------------------------------------
    async def _on_update(self, *args, **kwargs) -> None:
        """Stream-end / disconnect detection (signature is (client, update))."""
        update = args[-1] if args else kwargs.get("update")
        try:
            if isinstance(update, StreamEnded):
                chat_id = update.chat_id
                logger.info("UPDATE: StreamEnded in %s", chat_id)
                await self._on_track_end(chat_id)
                return
            if isinstance(update, ChatUpdate):
                status = getattr(update, "status", None)
                if status is not None:
                    logger.info(
                        "UPDATE: ChatUpdate in %s status=%s", update.chat_id, status
                    )
                    # admin ended the group call / streamer kicked → cancel everything
                    if bool(status & ChatUpdate.Status.CLOSED_VOICE_CHAT) or bool(
                        status & ChatUpdate.Status.KICKED
                    ):
                        await self._on_call_ended(update.chat_id)
                return
        except Exception:
            pass
        # also try raw update types (older/newer versions vary)
        try:
            if update and hasattr(update, "chat_id") and str(update).lower().find("streamend") >= 0:
                logger.info("UPDATE: raw stream-end in %s", update.chat_id)
                await self._on_track_end(update.chat_id)
        except Exception:
            pass

    async def _on_call_ended(self, chat_id: int) -> None:
        """Group call was ended (admin pressed end / streamer kicked) — cancel queue + state."""
        logger.info("CALL ENDED in %s — stopping queue", chat_id)
        self._created_calls.pop(chat_id, None)
        st = manager.get(chat_id)
        was_playing = st.playing or bool(st.queue) or st.current is not None
        old = self._playing_file.pop(chat_id, None)
        downloader.delete_file(old)
        manager.stop(chat_id)
        if was_playing:
            try:
                await self.app.send_message(
                    chat_id, "📴 **Group call ended** — playback stopped and queue cleared."
                )
            except Exception:
                pass

    async def _on_track_end(self, chat_id: int) -> None:
        logger.info("TRACK END in %s", chat_id)
        st = manager.get(chat_id)
        if st.loop and st.current:
            # loop ON: replay the same file (do NOT delete it)
            await self._safe_play(chat_id, st.current)
            return
        old = self._playing_file.pop(chat_id, None)
        downloader.delete_file(old)
        async with self._lock:
            nxt = manager.next_track(chat_id)
            if nxt is None:
                manager.set_current(chat_id, None)
                logger.info("Queue finished in chat %s", chat_id)
                # last track done → end the voice chat too
                try:
                    await self.pytgcalls.leave_call(chat_id, close=True)
                except Exception:
                    pass  # call may already be closed — fine
                await self._notify_finished(chat_id)
                return
            manager.set_current(chat_id, nxt)
        await self._safe_play(chat_id, nxt)

    # ------------------------------------------------------------------
    async def _call_active(self, chat_id: int) -> bool:
        """True when a voice/video chat is actually RUNNING in the group.

        Uses the call we TRACKED via _created_calls (or py-tgcalls' own
        cache) and probes it with phone.GetGroupCall — this avoids
        GetFullChannel/GetFullChat entirely, whose responses use TL
        constructors that pyrogram 2.0.106 (2023 schema) can't parse
        against Telegram's 2026 server ("unknown constructor" → desync →
        Request timed out).

        Priority:
          1. a call we created recently (in _created_calls) — probe it
          2. a call py-tgcalls cached — probe it
          3. fall back to GetFullChannel ONLY as a last resort, and any
             parse failure is treated as "call active" (safer: we then
             reuse/join instead of creating a duplicate).
        """
        from pyrogram.raw import functions
        from pyrogram.raw.types import InputGroupCall

        # 1. tracked call from our own CreateGroupCall
        last = self._created_calls.get(chat_id)
        if last and last[1] is not None:
            try:
                await self.app.invoke(functions.phone.GetGroupCall(call=last[1], limit=0))
                return True
            except Exception:
                pass  # GROUPCALL_INVALID → dead, fall through
        # 2. py-tgcalls cached call
        try:
            cached = self.pytgcalls._app._bind_client._cache.get_cache(chat_id)
            if cached is not None:
                try:
                    await self.app.invoke(functions.phone.GetGroupCall(call=cached, limit=0))
                    return True
                except Exception:
                    pass
        except Exception:
            pass
        # 3. last resort: ChatFull probe; parse failure ⇒ assume active
        try:
            from pyrogram.raw.types import InputPeerChannel, InputPeerChat

            peer = await self.app.resolve_peer(chat_id)
            if isinstance(peer, InputPeerChannel):
                full = await self.app.invoke(functions.channels.GetFullChannel(channel=peer))
            elif isinstance(peer, InputPeerChat):
                full = await self.app.invoke(functions.messages.GetFullChat(chat_id=peer.chat_id))
            else:
                return False
            call = getattr(full, "call", None)
            if call is None:
                return False
            # verify the call is actually joinable (not discarded)
            await self.app.invoke(functions.phone.GetGroupCall(call=call, limit=0))
            return True
        except Exception:
            # unknown constructor / any parse failure — cannot tell for
            # sure; assume a call may exist so we don't duplicate it.
            return True

    async def _ensure_call(self, chat_id: int) -> bool:
        """Create the group voice chat if none is active (auto-start call).

        Returns True when a call is joinable (either already running or
        freshly created). The fresh call ID from the CreateGroupCall response
        is injected straight into py-tgcalls' group-call cache, because
        GetFullChannel lags 10+s behind reality — waiting for it makes the
        very next join fail GROUPCALL_INVALID.

        IDEMPOTENT: tracks the last call we created per chat; if it's still
        fresh (< 90s) we reuse it instead of creating ANOTHER call. This
        prevents the "start → end → start" flicker when _prestart_call and
        _safe_play both run _ensure_call for the same play.
        """
        now = time.monotonic()
        from pyrogram.raw import functions
        last = self._created_calls.get(chat_id)
        if last and (now - last[0]) < 90:
            fresh_call = last[1]
            # VERIFY the tracked call is still joinable BEFORE reusing it —
            # a call can die within the 90s window (discarded/closed without
            # a ChatUpdate), and joining a dead ID fails GROUPCALL_INVALID
            # forever (the retry loop can't break out). If dead → drop it.
            if fresh_call is not None:
                try:
                    await self.app.invoke(
                        functions.phone.GetGroupCall(call=fresh_call, limit=0)
                    )
                except Exception:
                    # dead call — forget it, we'll create a fresh one below
                    self._created_calls.pop(chat_id, None)
                    try:
                        self.pytgcalls._app._bind_client._cache.drop_cache(chat_id)
                    except Exception:
                        pass
                else:
                    # live — re-inject into py-tgcalls cache and reuse
                    try:
                        self.pytgcalls._app._bind_client._cache.set_cache(chat_id, fresh_call)
                    except Exception:
                        pass
                    return True
        if await self._call_active(chat_id):
            # a call is verified live. _call_active already probed the
            # tracked/py-tgcalls-cached call and (if we created it) it's in
            # _created_calls → the cache is current. No GetFullChannel here:
            # its responses can't be parsed with the 2023 pyrogram schema.
            return True
        result = await self.app.invoke(
            CreateGroupCall(
                peer=await self.app.resolve_peer(chat_id),
                random_id=random.getrandbits(31),
                rtmp_stream=False,
            )
        )
        # pull the freshly created call out of the response updates
        fresh_call = None
        for update in result.updates:
            if isinstance(update, UpdateGroupCall) and isinstance(update.call, GroupCall):
                fresh_call = InputGroupCall(
                    id=update.call.id,
                    access_hash=update.call.access_hash,
                )
                break
        if fresh_call is not None:
            try:
                self.pytgcalls._app._bind_client._cache.set_cache(chat_id, fresh_call)
            except Exception:
                pass
        # remember we created this call so a second _ensure_call within 90s
        # reuses it instead of starting a fresh one (kills start/end/start)
        self._created_calls[chat_id] = (time.monotonic(), fresh_call)
        await asyncio.sleep(2.5)  # let the call actually start
        return fresh_call is not None

    async def _safe_play(self, chat_id: int, track: Track) -> None:
        try:
            # no active call? start one automatically (voice chat)
            try:
                await self._ensure_call(chat_id)
            except Exception as e:
                logger.warning("create call in %s: %s — letting auto_start handle it", chat_id, e)
            await self.pytgcalls.play(
                chat_id,
                MediaStream(
                    track.file_path,
                    audio_parameters=AudioQuality.HIGH,
                    video_parameters=VideoQuality.HD_720p if track.is_video else VideoQuality.SD_360p,
                ),
                GroupCallConfig(auto_start=True),
            )
            self._playing_file[chat_id] = track.file_path
            manager.set_paused(chat_id, False)
            logger.info("Now playing in %s: %s", chat_id, track.title)
            await self._save_to_library(track)
        except Exception as e:
            logger.error("play failed in %s: %s", chat_id, e)
            # Robust recovery: a dead/closed call needs time to propagate —
            # retry a few times, recreating a fresh call each round.
            for attempt in range(3):
                try:
                    # Drop py-tgcalls' internal group-call cache: after a call
                    # dies externally it still holds the DEAD call ID, and
                    # JoinGroupCall keeps failing GROUPCALL_INVALID on it even
                    # after we create a fresh call. NOTE: the cache lives on
                    # the bridged client (_bind_client), NOT on MtProtoClient.
                    try:
                        self.pytgcalls._app._bind_client._cache.drop_cache(chat_id)
                    except Exception:
                        pass
                    # leave quietly — GROUPCALL_FORBIDDEN (not a participant)
                    # is fine: nothing to leave, don't abort the retry.
                    try:
                        await self.pytgcalls.leave_call(chat_id, close=False)
                    except Exception as leave_e:
                        logger.warning(
                            "leave before retry %d in %s: %s",
                            attempt + 1, chat_id, leave_e,
                        )
                    await asyncio.sleep(3)  # let the old call fully die
                    # ALWAYS create a fresh call on retry (injects its ID
                    # straight into py-tgcalls' cache) — do not trust
                    # _call_active here, it only probes ChatFull which lags.
                    try:
                        await self._ensure_call(chat_id)
                    except Exception as ensure_e:
                        logger.warning(
                            "ensure_call retry %d in %s: %s",
                            attempt + 1, chat_id, ensure_e,
                        )
                    await asyncio.sleep(3)  # propagation before joining
                    await self.pytgcalls.play(
                        chat_id,
                        MediaStream(
                            track.file_path,
                            audio_parameters=AudioQuality.HIGH,
                            video_parameters=VideoQuality.HD_720p
                            if track.is_video else VideoQuality.SD_360p,
                        ),
                        GroupCallConfig(auto_start=True),
                    )
                    manager.set_paused(chat_id, False)
                    self._playing_file[chat_id] = track.file_path
                    logger.info(
                        "Now playing (retry %d) in %s: %s",
                        attempt + 1, chat_id, track.title,
                    )
                    return
                except Exception as retry_e:
                    logger.warning(
                        "retry %d failed in %s: %s",
                        attempt + 1, chat_id, retry_e,
                    )
                    await asyncio.sleep(2)
            # all retries exhausted — clean up and notify
            old = self._playing_file.pop(chat_id, None)
            downloader.delete_file(old)
            manager.stop(chat_id)
            try:
                await self.app.send_message(
                    chat_id,
                    "❌ **Could not start playback.**\n\n"
                    "Make sure the **bot** is an admin here, the voice chat is open, "
                    f"and the streamer is a member.\n`{e}`",
                )
            except Exception:
                pass

    async def play_track(self, chat_id: int, track: Track) -> bool:
        """Play immediately (queue start) or queue if already playing.

        Self-heals stale state: if the manager thinks something is playing but no
        voice chat is actually running, reset and start fresh instead of queuing.
        """
        st = manager.get(chat_id)
        if st.playing and not await self._call_active(chat_id):
            logger.warning("stale playing state in %s (no active call) — resetting", chat_id)
            manager.stop(chat_id)
            st = manager.get(chat_id)
        if st.playing:
            return False
        manager.set_current(chat_id, track)
        await self._safe_play(chat_id, track)
        return True

    # ------------------------------------------------------------------
    async def pause(self, chat_id: int) -> bool:
        ok = await self.pytgcalls.pause(chat_id)
        manager.set_paused(chat_id, True)
        return ok

    async def resume(self, chat_id: int) -> bool:
        ok = await self.pytgcalls.resume(chat_id)
        manager.set_paused(chat_id, False)
        return ok

    async def stop(self, chat_id: int) -> None:
        """Stop playback, clear queue, end the voice chat."""
        old = self._playing_file.pop(chat_id, None)
        downloader.delete_file(old)
        manager.stop(chat_id)
        try:
            await self.pytgcalls.leave_call(chat_id, close=True)
        except Exception:
            pass

    async def set_volume(self, chat_id: int, volume: int) -> int:
        volume = manager.set_volume(chat_id, volume)
        try:
            await self.pytgcalls.change_volume_call(chat_id, volume)
        except Exception as e:
            logger.debug("volume change failed: %s", e)
        return volume

    async def seek(self, chat_id: int, delta: int) -> int:
        """Seek the current track ±delta seconds.

        py-tgcalls has no native seek — we re-start the same local file with
        ffmpeg's `-ss <pos>` INPUT seek (fast, accurate on local files),
        clamped to [0, duration]. Returns the new position in seconds.
        """
        st = manager.get(chat_id)
        if not st.playing or not st.current or not st.current.file_path:
            return -1
        track = st.current
        new_pos = max(0, st.seek_pos + delta)
        if track.duration and new_pos >= track.duration:
            new_pos = max(0, track.duration - 1)  # clamp just before the end
        if new_pos == st.seek_pos and delta != 0:
            return st.seek_pos
        manager.set_seek(chat_id, new_pos)
        # LIVE SEEK: pytgcalls.play() when already in a call does NOT
        # leave/rejoin — it swaps the stream source in-place via
        # set_stream_sources(CAPTURE). So we just play() a fresh MediaStream
        # with ffmpeg `-ss <pos>` input seek. No call restart, no disconnect
        # flicker — the stream keeps playing and jumps to the new position.
        try:
            ss = f"-ss {int(new_pos)} " if new_pos > 0 else None
            await self.pytgcalls.play(
                chat_id,
                MediaStream(
                    track.file_path,
                    audio_parameters=AudioQuality.HIGH,
                    video_parameters=VideoQuality.HD_720p
                    if track.is_video else VideoQuality.SD_360p,
                    ffmpeg_parameters=ss,
                ),
                GroupCallConfig(auto_start=True),
            )
            st = manager.get(chat_id)
            if st.paused:
                try:
                    await self.pytgcalls.pause(chat_id)
                except Exception:
                    pass
            self._playing_file[chat_id] = track.file_path
            logger.info("Live seek in %s → %ss (%s)", chat_id, new_pos, track.title)
            return new_pos
        except Exception as e:
            logger.error("seek failed in %s: %s", chat_id, e)
            manager.reset_seek(chat_id)
            return -1

    async def leave(self, chat_id: int) -> None:
        old = self._playing_file.pop(chat_id, None)
        downloader.delete_file(old)
        manager.stop(chat_id)
        try:
            await self.pytgcalls.leave_call(chat_id, close=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    async def _save_to_library(self, track: Track) -> None:
        """Persist every played track so it can be replayed later (💾 Saved)."""
        try:
            from handlers.context import DB  # runtime import avoids import cycle

            if track.stream_url:
                source, url, file_id = "youtube", track.stream_url, ""
            elif track.file_id:
                source, url, file_id = "telegram", "", track.file_id
            else:
                return  # nothing replayable
            await DB.save_track(
                title=track.title,
                source=source,
                url=url,
                file_id=file_id,
                is_video=track.is_video,
                duration=track.duration,
                requester_id=track.requester_id,
            )
        except Exception as e:  # library must never break playback
            logger.warning("save_track failed: %s", e)

    # ------------------------------------------------------------------
    async def _notify_finished(self, chat_id: int) -> None:
        try:
            await self.app.send_message(chat_id, "✅ Queue finished — playback ended.")
        except Exception:
            pass

    async def active_calls(self) -> list:
        out = []
        for chat_id in manager.active_chats():
            try:
                parts = await self.pytgcalls.get_participants(chat_id)
                out.append({"chat_id": chat_id, "participants": len(parts)})
            except Exception:
                out.append({"chat_id": chat_id, "participants": 0})
        return out


def cleanup_all_playing(streamer: Streamer) -> None:
    for path in list(streamer._playing_file.values()):
        downloader.delete_file(path)
    streamer._playing_file.clear()
