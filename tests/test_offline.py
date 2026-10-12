"""Offline tests: no API keys or audio hardware needed."""
import asyncio, json, time
import numpy as np
from pipecat.frames.frames import (LLMTextFrame, LLMFullResponseEndFrame, TTSAudioRawFrame,
    TTSStoppedFrame, InputAudioRawFrame, LLMMessagesUpdateFrame, LLMMessagesAppendFrame)
from pipecat.tests.utils import run_test

from demon.safety import SentenceSafetyFilter, clean_sentence, is_blocked
from demon.fx import DemonVoiceFX
from demon.presence import MicGate, PresenceController, State
from demon import persona


def texts(frames):
    return "".join(f.text for f in frames if isinstance(f, LLMTextFrame))


async def test_safety_filter():
    f = SentenceSafetyFilter("FALLBACK.")
    down, _ = await run_test(f, frames_to_send=[
        LLMTextFrame("Welcome, mor"), LLMTextFrame("tal! *evil cackle* Are you "),
        LLMTextFrame("brave tonight?"), LLMFullResponseEndFrame()])
    out = texts(down)
    assert out.strip() == "Welcome, mortal! Are you brave tonight?", out

    f = SentenceSafetyFilter("FALLBACK.")
    down, _ = await run_test(f, frames_to_send=[
        LLMTextFrame("Hello there. I will kill you. "), LLMTextFrame("And more."),
        LLMFullResponseEndFrame()])
    out = texts(down)
    assert out.strip() == "Hello there. FALLBACK.", out
    assert not is_blocked("Hello skillful undead hellhound fans")  # no false positives on substrings
    assert clean_sentence("Ha (laughs) [grins] ha!") == "Ha ha!"
    # v3/v4 audio tags: whitelisted ones survive (normalised), others are stripped
    tags = ("laughs", "evil laugh")
    assert clean_sentence("[Evil Laugh] Welcome [grins] mortal!", tags) == "[evil laugh] Welcome mortal!"
    assert clean_sentence("Ha [Stage direction: x] ha.", tags) == "Ha ha."
    f = SentenceSafetyFilter("FALLBACK.", allowed_tags=tags)
    down, _ = await run_test(f, frames_to_send=[LLMTextFrame("Welcome! [laughs]"), LLMFullResponseEndFrame()])
    assert texts(down).strip() == "Welcome! [laughs]", texts(down)
    # a reply cut off mid-word by max_tokens loses its unfinished tail
    f = SentenceSafetyFilter("FALLBACK.")
    down, _ = await run_test(f, frames_to_send=[
        LLMTextFrame("Hello there. Mind the cau"), LLMFullResponseEndFrame()])
    assert texts(down).strip() == "Hello there.", texts(down)
    print("safety filter OK")


async def test_fx():
    fx = DemonVoiceFX(preset="demon")
    sr = 16000
    t = np.arange(sr // 10) / sr
    chunks = []
    for i in range(10):  # 1 s of 220 Hz in 100 ms chunks, like streamed TTS
        x = (0.5 * np.sin(2 * np.pi * 220 * (t + i * 0.1)) * 32767).astype(np.int16).tobytes()
        chunks.append(TTSAudioRawFrame(audio=x, sample_rate=sr, num_channels=1))
    t0 = time.perf_counter()
    down, _ = await run_test(fx, frames_to_send=chunks + [TTSStoppedFrame()])
    dt = time.perf_counter() - t0
    audio = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert len(audio) == 11, len(audio)  # 10 chunks + flush tail
    y = np.frombuffer(b"".join(f.audio for f in audio), dtype=np.int16).astype(float)
    assert np.abs(y).max() > 1000, "FX output is silent"
    first = np.frombuffer(audio[0].audio, dtype=np.int16)
    assert np.abs(first[800:]).max() > 1000, "pitch shifter adds too much delay"  # sound within 50 ms
    # dominant frequency should have dropped ~4 semitones (220 -> ~175 Hz)
    seg = y[sr // 2: sr // 2 + sr // 2]
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    peak = np.fft.rfftfreq(len(seg), 1 / sr)[spec.argmax()]
    assert 160 < peak < 190, peak
    # pure shifter check, no other effects
    from demon.fx import DopplerPitchShifter
    sh = DopplerPitchShifter(-4, sr); x = 0.5*np.sin(2*np.pi*220*np.arange(sr)/sr).astype(np.float32)
    z = np.concatenate([sh.process(x[i:i+160]) for i in range(0, sr, 160)])[sr//2:]
    pk = np.fft.rfftfreq(len(z), 1/sr)[np.abs(np.fft.rfft(z*np.hanning(len(z)))).argmax()]
    assert abs(pk - 220*2**(-4/12)) < 6, pk
    print(f"FX OK: 220 Hz -> {peak:.0f} Hz, 1 s of audio processed in {dt*1000:.0f} ms (x86)")


class FakeTask:
    def __init__(self): self.queued = []
    async def queue_frames(self, frames): self.queued += frames


async def test_presence():
    task, gate = FakeTask(), MicGate()
    c = PresenceController(task, gate, engage_dist=250, leave_secs=0.3, max_session_secs=60)
    await c._on_presence(json.dumps({"count": 2, "nearest": 700}).encode())
    assert c.state == State.IDLE and not gate.is_open  # too far away
    await c._on_presence(json.dumps({"count": 2, "nearest": 120}).encode())
    assert c.state == State.ENGAGED and gate.is_open
    f = task.queued[-1]
    assert isinstance(f, LLMMessagesUpdateFrame) and f.run_llm and "group of 2" in f.messages[0]["content"]
    await asyncio.sleep(0.4); await c._tick()
    assert c.state == State.IDLE and not gate.is_open
    assert isinstance(task.queued[-1], LLMMessagesUpdateFrame) and task.queued[-1].messages == []
    # session limit -> farewell + cooldown
    c._max_session = 0.0
    await c._on_presence(b'{"count": 1}')
    await c._tick()
    assert c.state == State.COOLDOWN and isinstance(task.queued[-1], LLMMessagesAppendFrame)
    await c._on_presence(b"garbage")  # must not crash
    print("presence OK")


async def test_gate():
    g = MicGate(open_=False)
    a = InputAudioRawFrame(audio=bytes(320), sample_rate=16000, num_channels=1)
    down, _ = await run_test(g, frames_to_send=[a], expected_down_frames=[])
    g.is_open = True
    down, _ = await run_test(g, frames_to_send=[a], expected_down_frames=[InputAudioRawFrame])
    print("mic gate OK")


def test_tts_selection():
    import os
    os.environ.update(ELEVENLABS_API_KEY="x", ELEVENLABS_VOICE_ID="v")
    import bot
    from pipecat.services.elevenlabs.dialogue.tts import ElevenLabsDialogueTTSService
    from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
    for model, cls in [("eleven_flash_v2_5", ElevenLabsTTSService),
                       ("eleven_v4_turbo", ElevenLabsDialogueTTSService)]:
        os.environ["ELEVENLABS_MODEL"] = model
        tts = bot.build_tts(16000)
        assert type(tts) is cls and tts._settings.model == model, (model, type(tts))
    assert "[evil laugh]" in persona.system_prompt(True) and "[" not in persona.system_prompt(False).split("HOW YOU TALK")[1].split("STRICT")[0]
    print("TTS model selection OK")


async def main():
    test_tts_selection()
    await test_safety_filter(); await test_fx(); await test_presence(); await test_gate()
    print("ALL PASSED")

asyncio.run(main())
