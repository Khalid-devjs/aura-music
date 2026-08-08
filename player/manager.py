"""
Per-chat playback state: queues, current track, loop, volume.
One instance shared across all handlers; keyed by chat_id.
"""
import threading
from dataclasses import dataclass, field
from typing import Optional

import config


@dataclass
class Track:
    title: str = ""
    duration: int = 0            # seconds, 0 = unknown
    file_path: str = ""          # local cached file
    source: str = ""             # url / query / file name
    requester_id: int = 0
    requester_name: str = ""
    is_video: bool = False
    thumbnail: str = ""
    stream_url: str = ""
    file_id: str = ""            # Telegram file_id (for later re-download / saved library)


@dataclass
class ChatState:
    chat_id: int
    queue: list = field(default_factory=list)      # list[Track]
    current: Optional[Track] = None
    loop: bool = False
    volume: int = config.DEFAULT_VOLUME
    paused: bool = False
    playing: bool = False
    player_msg = None
    playlist_pos: int = 0


class PlayerManager:
    def __init__(self):
        self._states: dict[int, ChatState] = {}
        self._lock = threading.Lock()

    def get(self, chat_id: int) -> ChatState:
        with self._lock:
            if chat_id not in self._states:
                self._states[chat_id] = ChatState(chat_id=chat_id)
            return self._states[chat_id]

    def add_track(self, chat_id: int, track: Track) -> int:
        """Append a track; returns new queue length."""
        st = self.get(chat_id)
        with self._lock:
            st.queue.append(track)
            return len(st.queue)

    def next_track(self, chat_id: int, force: bool = False) -> Optional[Track]:
        """Pop the next track. Loop-aware unless force=True (explicit skip)."""
        st = self.get(chat_id)
        with self._lock:
            if st.loop and st.current and not force:
                return st.current
            if st.queue:
                return st.queue.pop(0)
            return None

    def clear_queue(self, chat_id: int) -> int:
        st = self.get(chat_id)
        with self._lock:
            n = len(st.queue)
            st.queue.clear()
            return n

    def set_current(self, chat_id: int, track: Optional[Track]) -> None:
        st = self.get(chat_id)
        with self._lock:
            st.current = track
            st.playing = track is not None

    def set_loop(self, chat_id: int, value: bool) -> bool:
        st = self.get(chat_id)
        with self._lock:
            st.loop = value
            return st.loop

    def set_paused(self, chat_id: int, value: bool) -> None:
        st = self.get(chat_id)
        with self._lock:
            st.paused = value

    def set_volume(self, chat_id: int, volume: int) -> int:
        st = self.get(chat_id)
        with self._lock:
            st.volume = max(0, min(200, volume))
            return st.volume

    def stop(self, chat_id: int) -> None:
        st = self.get(chat_id)
        with self._lock:
            st.queue.clear()
            st.current = None
            st.paused = False
            st.playing = False
            st.loop = False

    def state_snapshot(self, chat_id: int) -> dict:
        st = self.get(chat_id)
        return {
            "paused": st.paused,
            "loop": st.loop,
            "volume": st.volume,
            "has_queue": len(st.queue) > 0,
        }

    def active_chats(self) -> list:
        with self._lock:
            return [c for c, st in self._states.items() if st.playing]

    def queue_len(self, chat_id: int) -> int:
        return len(self.get(chat_id).queue)


manager = PlayerManager()
