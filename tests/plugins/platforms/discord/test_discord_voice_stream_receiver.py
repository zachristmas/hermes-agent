"""Barge-in from real voice packets: libopus-encoded speech, NaCl AEAD (rtpsize) encrypted into RTP,
through the production ``VoiceReceiver._on_packet`` decrypt -> decode path into the adapter's sink.

Real codec and cipher, fake transport: this is not live Discord evidence (no DAVE, no gateway SSRC
mapping, no network jitter)."""

import array
import asyncio
import math
import os
import struct
import sys
import time
from unittest.mock import MagicMock

import pytest

from discord_voice_fakes import ALLOWED_USER, GUILD, STRANGER, TEXT_CHANNEL, FakeVoiceClient

from gateway.platforms.base import AudioFormat

discord = pytest.importorskip("discord")
nacl_secret = pytest.importorskip("nacl.secret")

FMT = AudioFormat(sample_rate=24000, channels=1, sample_width=2)
CHAT = str(TEXT_CHANNEL)
BOT_SSRC, USER_SSRC, STRANGER_SSRC = 7, 1000, 2000


@pytest.fixture(scope="module")
def opus_encoder():
    if not discord.opus.is_loaded():
        try:
            discord.opus._load_default()
        except Exception:
            pass
    if not discord.opus.is_loaded():
        pytest.skip("libopus not available")
    return discord.opus.Encoder()


def _speech_pcm(frame_index, amplitude=8000):
    arr = array.array("h")
    for i in range(960):
        v = int(amplitude * math.sin(2 * math.pi * 300 * (frame_index * 960 + i) / 48000))
        arr.extend((v, v))
    if sys.byteorder == "big":
        arr.byteswap()
    return arr.tobytes()


def _rtp(key, ssrc, seq, opus_payload):
    header = struct.pack(">BBHII", 0x80, 0x78, seq, seq * 960, ssrc)
    nonce4 = struct.pack(">I", seq)
    sealed = nacl_secret.Aead(key).encrypt(opus_payload, header, nonce4 + b"\x00" * 20)
    return header + sealed.ciphertext + nonce4


@pytest.fixture
def wired(make_adapter):
    """Adapter + a production VoiceReceiver whose decoded frames feed the adapter's sink."""
    from plugins.platforms.discord.adapter import VoiceReceiver

    adapter, vc = make_adapter(vc=FakeVoiceClient(frame_interval=0.01))
    key = os.urandom(32)
    receiver = VoiceReceiver(vc, allowed_user_ids=adapter._allowed_user_ids)
    receiver._creds = (key, None, 0, False)
    receiver._bot_ssrc = BOT_SSRC
    receiver._running = True
    receiver.map_ssrc(USER_SSRC, ALLOWED_USER)
    receiver.map_ssrc(STRANGER_SSRC, STRANGER)
    receiver.frame_sink = adapter._voice_frame_sink(GUILD)
    adapter._voice_receivers[GUILD] = receiver
    return adapter, vc, receiver, key


async def _send(receiver, key, encoder, ssrc, frames):
    packets = [_rtp(key, ssrc, seq, encoder.encode(_speech_pcm(seq), 960)) for seq in range(1, frames + 1)]

    def _socket_thread():
        for packet in packets:
            receiver._on_packet(packet)
            time.sleep(0.002)

    await asyncio.get_running_loop().run_in_executor(None, _socket_thread)
    await asyncio.sleep(0.05)


def _level_pcm(level, ms):
    arr = array.array("h", [level] * (24 * ms))
    if sys.byteorder == "big":
        arr.byteswap()
    return arr.tobytes()


@pytest.mark.asyncio
async def test_allowed_speakers_encrypted_opus_cuts_the_turn(wired, opus_encoder):
    adapter, vc, receiver, key = wired
    handle = await adapter.begin_streaming_tts(CHAT, FMT)
    await adapter.write_streaming_tts(handle, _level_pcm(1000, 1000))
    await _send(receiver, key, opus_encoder, USER_SSRC, frames=25)
    assert receiver._decode_ok >= 15 and receiver._decode_failed == 0
    assert handle.cut_reason == "barge-in" and handle.aborted
    # The same frames still reached the utterance buffer for normal transcription.
    assert len(receiver._buffers[USER_SSRC]) >= 15 * 3840


@pytest.mark.asyncio
async def test_strangers_and_the_bots_own_ssrc_never_cut(wired, opus_encoder):
    adapter, vc, receiver, key = wired
    handle = await adapter.begin_streaming_tts(CHAT, FMT)
    await adapter.write_streaming_tts(handle, _level_pcm(1000, 400))
    await _send(receiver, key, opus_encoder, STRANGER_SSRC, frames=25)
    await _send(receiver, key, opus_encoder, BOT_SSRC, frames=25)
    assert receiver._decode_ok == 25  # the stranger decoded; the bot's SSRC was dropped first
    assert handle.cut_reason == ""
    await adapter.finish_streaming_tts(handle)


@pytest.mark.asyncio
async def test_garbled_packets_do_not_reach_the_sink(wired, opus_encoder):
    adapter, vc, receiver, key = wired
    handle = await adapter.begin_streaming_tts(CHAT, FMT)
    await adapter.write_streaming_tts(handle, _level_pcm(1000, 400))
    receiver._creds = (os.urandom(32), None, 0, False)  # wrong transport key: every decrypt fails
    receiver.refresh_credentials = MagicMock()
    await _send(receiver, key, opus_encoder, USER_SSRC, frames=25)
    assert receiver._decode_ok == 0 and handle.cut_reason == ""
    await adapter.finish_streaming_tts(handle)
