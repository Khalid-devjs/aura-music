"""
PyTgCalls streaming wrapper.

Bound to the USER-account Pyrogram client (voice chats are user-only).
Manages: join/play/pause/resume/stop/volume, auto-advance on StreamEnded,
re-join on unexpected disconnect, file cleanup after each track.
"""
import asyncio
import logging
import random

from pyrogram import Client
from pyrogram.raw.functions.phone import CreateGroupCall
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
                await self._on_track_end(chat_id)
                return
            if isinstance(update, ChatUpdate):
                status = getattr(update, "status", None)
                if status is not None:
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
                await self._on_track_end(update.chat_id)
        except Exception:
            pass

    async def _on_call_ended(self, chat_id: int) -> None:
        """Group call was ended (admin pressed end / streamer kicked) — cancel queue + state."""
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
        """True when a voice/video chat is actually running in the group.

        Uses raw MTProto: ChatFull.call is a GroupCall when a call is live,
        None when no (or ended) call. Supergroups = GetFullChannel, basic
        groups = GetFullChat.
        """
        try:
            from pyrogram.raw import functions
            from pyrogram.raw.types import InputPeerChannel, InputPeerChat

            peer = await self.app.resolve_peer(chat_id)
            if isinstance(peer, InputPeerChannel):
                full = await self.app.invoke(functions.channels.GetFullChannel(channel=peer))
            elif isinstance(peer, InputPeerChat):
                full = await self.app.invoke(functions.messages.GetFullChat(chat_id=peer.chat_id))
            else:
                return False
            return getattr(full, "call", None) is not None
        except Exception:
            return False

    async def _ensure_call(self, chat_id: int) -> None:
        """Create the group voice chat if none is active (auto-start call)."""
        if await self._call_active(chat_id):
            return
        await self.app.invoke(
            CreateGroupCall(
                peer=await self.app.resolve_peer(chat_id),
                random_id=random.randint(1, 2**31),
                rtmp_stream=False,
            )
        )
        await asyncio.sleep(2.5)  # let the call actually start

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
            # re-join attempt (auto reconnect) — fresh call if the old one died
            try:
                await self.pytgcalls.leave_call(chat_id, close=False)
                await asyncio.sleep(1)
                if not await self._call_active(chat_id):
                    await self._ensure_call(chat_id)
                await self.pytgcalls.play(
                    chat_id,
                    MediaStream(
                        track.file_path,
                        audio_parameters=AudioQuality.HIGH,
                        video_parameters=VideoQuality.HD_720p if track.is_video else VideoQuality.SD_360p,
                    ),
                    GroupCallConfig(auto_start=True),
                )
                manager.set_paused(chat_id, False)
            except Exception as e2:
                logger.error("rejoin failed in %s: %s", chat_id, e2)
                old = self._playing_file.pop(chat_id, None)
                downloader.delete_file(old)
                manager.stop(chat_id)
                try:
                    if "CHAT_ADMIN_REQUIRED" in str(e2):
                        await self.app.send_message(
                            chat_id,
                            "❌ **Could not start the voice chat** — the streamer isn't an admin here.\n\n"
                            "Give the **bot full admin** rights in this group (especially **Manage voice chats**), "
                            "or set **Who can start voice chats** to *All members* in group settings.\n"
                            "If **@teleaddbyking** shows as banned, unban it (group settings → Members).",
                        )
                    else:
                        await self.app.send_message(
                            chat_id,
                            "❌ **Could not start playback.**\n\n"
                            "Make sure the **bot** is an admin here, the voice chat is open, "
                            f"and the streamer is a member.\n`{e2}`",
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
