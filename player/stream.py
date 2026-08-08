"""
PyTgCalls streaming wrapper.

Bound to the USER-account Pyrogram client (voice chats are user-only).
Manages: join/play/pause/resume/stop/volume, auto-advance on StreamEnded,
re-join on unexpected disconnect, file cleanup after each track.
"""
import asyncio
import logging

from pyrogram import Client
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
        self.pytgcalls.on_update(self._on_update)
        await self.pytgcalls.start()
        logger.info("PyTgCalls started")

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
                await self._notify_finished(chat_id)
                return
            manager.set_current(chat_id, nxt)
        await self._safe_play(chat_id, nxt)

    # ------------------------------------------------------------------
    async def _safe_play(self, chat_id: int, track: Track) -> None:
        try:
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
            # re-join attempt (auto reconnect)
            try:
                await self.pytgcalls.leave_call(chat_id, close=False)
                await asyncio.sleep(2)
                await self.pytgcalls.play(
                    chat_id,
                    MediaStream(track.file_path),
                    GroupCallConfig(auto_start=True),
                )
                manager.set_paused(chat_id, False)
            except Exception as e2:
                logger.error("rejoin failed in %s: %s", chat_id, e2)
                old = self._playing_file.pop(chat_id, None)
                downloader.delete_file(old)
                manager.stop(chat_id)
                try:
                    await self.app.send_message(
                        chat_id,
                        "❌ **Could not start playback.**\n\n"
                        "Make sure the **boss bot** is an admin here, the voice chat is open, "
                        f"and the streamer is a member.\n`{e2}`",
                    )
                except Exception:
                    pass

    async def play_track(self, chat_id: int, track: Track) -> bool:
        """Play immediately (queue start) or queue if already playing."""
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
