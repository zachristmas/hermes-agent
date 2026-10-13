"""Schema-faithful fakes at the Discord audio boundary for the streaming-voice tests.

``FakeVoiceClient`` mirrors the discord.py ``VoiceClient`` surface the adapter uses (``play`` refuses
a non-``AudioSource`` and a second source, ``stop``, ``pause``, ``source``, ``is_playing``,
``is_paused``, ``is_connected``) and runs a real background player thread shaped like
``discord.player.AudioPlayer``: it pulls ``source.read()`` every frame, stops on ``b""``, then calls ``after(error)`` and ``source.cleanup()``.
These are fakes: passing tests are not live Discord evidence.
"""

import threading
import time
from unittest.mock import MagicMock

import pytest

discord = pytest.importorskip("discord")


class FakePlayer(threading.Thread):
    def __init__(self, source, after, frame_interval, sent):
        super().__init__(daemon=True)
        self.source, self.after, self.frame_interval, self.sent = source, after, frame_interval, sent
        self._stop_event = threading.Event()
        self.resume = threading.Event()
        self.resume.set()
        self.paused = False  # discord.py's AudioPlayer.pause(): still alive, just not playing

    def run(self):
        error = None
        try:
            while not self._stop_event.is_set():
                self.resume.wait()
                while self.paused and not self._stop_event.is_set():
                    time.sleep(0.002)
                if self._stop_event.is_set():
                    break
                data = self.source.read()
                if not data:
                    break
                self.sent.append(data)
                time.sleep(self.frame_interval)
        except Exception as exc:  # AudioPlayer records the error and reports it to ``after``
            error = exc
        finally:
            if self.after is not None:
                self.after(error)
            self.source.cleanup()

    def stop(self):
        self._stop_event.set()
        self.resume.set()
        self.paused = False


class FakeVoiceClient:
    def __init__(self, *, frame_interval=0.002, user_id=1):
        self.connected = True
        self.frame_interval = frame_interval
        self.sent = []  # every 20 ms frame handed to the (fake) encoder, in order
        self.plays = 0
        self._player = None
        self.user = MagicMock(id=user_id)
        self.channel = MagicMock(members=[])

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self._player is not None and self._player.is_alive() and not self._player.paused

    def is_paused(self):
        return self._player is not None and self._player.is_alive() and self._player.paused

    def pause(self):
        if self._player is not None:
            self._player.paused = True

    def resume(self):
        if self._player is not None:
            self._player.paused = False

    @property
    def source(self):
        return self._player.source if self._player is not None else None

    def play(self, source, *, after=None, **_kwargs):
        if not self.connected:
            raise discord.ClientException("Not connected to voice.")
        if not isinstance(source, discord.AudioSource):
            raise TypeError(f"source must be an AudioSource not {source.__class__.__name__}")
        if self.is_playing():
            raise discord.ClientException("Already playing audio.")
        self.plays += 1
        self._player = FakePlayer(source, after, self.frame_interval, self.sent)
        self._player.start()

    def stop(self):
        if self._player is not None:
            self._player.stop()
            self._player = None

    async def disconnect(self, *, force=False):
        self.stop()
        self.connected = False

    def stall(self):
        """The player thread stops pulling frames (a wedged connection)."""
        self._player.resume.clear()

    def wait_idle(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while self.is_playing() and time.monotonic() < deadline:
            time.sleep(0.005)
        return not self.is_playing()


GUILD, TEXT_CHANNEL, ALLOWED_USER, STRANGER = 111, 222, 42, 99
