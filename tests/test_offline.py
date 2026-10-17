"""Offline tests: no API keys or audio hardware needed."""
import asyncio, json, time
import numpy as np
from pipecat.frames.frames import (LLMTextFrame, LLMFullResponseEndFrame, TTSAudioRawFrame,
    TTSStoppedFrame, InputAudioRawFrame, LLMMessagesUpdateFrame, LLMMessagesAppendFrame,
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, TranscriptionFrame, InterimTranscriptionFrame)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.tests.utils import run_test

from demon.safety import SentenceSafetyFilter, clean_sentence, is_blocked
from demon.fx import DemonVoiceFX
from demon.presence import MicGate, PresenceController, State
from demon.admin import AdminCommands, split_magic
from demon.lights import BALL_CUES, CrystalBall, Light, in_hours, parse_hours
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
    # mid-chat head count changes are passed on once they've held steady
    task, gate = FakeTask(), MicGate()
    c = PresenceController(task, gate, engage_dist=250, leave_secs=5, max_session_secs=60)
    c.COUNT_SETTLE_SECS = 0.1
    await c._on_presence(b'{"count": 2}')
    await c._on_presence(b'{"count": 3}')  # first sighting: not yet
    assert len(task.queued) == 1
    await c._on_presence(b'{"count": 2}')  # flicker back: nothing to say
    await asyncio.sleep(0.15); await c._on_presence(b'{"count": 2}')
    assert len(task.queued) == 1
    await c._on_presence(b'{"count": 3}')
    await asyncio.sleep(0.15); await c._on_presence(b'{"count": 3}')
    f = task.queued[-1]
    assert isinstance(f, LLMMessagesAppendFrame) and not f.run_llm and "3 people" in f.messages[0]["content"]
    print("presence OK")


def test_situation():
    from datetime import datetime
    s = persona.arrival_direction(3, datetime(2026, 10, 13, 20, 5))
    assert "Tuesday 13 October, 20:05" in s and "crystal ball" in s and "after dark" in s and "18 days away" in s and "group of 3" in s
    assert "daylight" in persona.situation(datetime(2026, 10, 13, 15, 0))
    assert "not yet dark" in persona.situation(datetime(2026, 10, 13, 18, 30))
    assert "Halloween night itself" in persona.situation(datetime(2026, 10, 31, 19, 0))
    assert "tomorrow" in persona.situation(datetime(2026, 10, 30, 19, 0))
    assert "2 days ago" in persona.situation(datetime(2026, 11, 2, 19, 0))
    print("situation OK")


async def test_gate():
    g = MicGate(open_=False)
    a = InputAudioRawFrame(audio=bytes(320), sample_rate=16000, num_channels=1)
    down, _ = await run_test(g, frames_to_send=[a], expected_down_frames=[])
    g.is_open = True
    down, _ = await run_test(g, frames_to_send=[a], expected_down_frames=[InputAudioRawFrame])
    # while she speaks (and for the tail after), Deepgram gets silence instead of her voice
    g = MicGate(open_=True, mute_while_speaking=True, tail_secs=0.2)
    loud = InputAudioRawFrame(audio=b"\x10\x20" * 160, sample_rate=16000, num_channels=1)
    await g.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    assert g._hearing_bot()
    await g.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
    assert g._hearing_bot()  # tail
    down, _ = await run_test(g, frames_to_send=[loud], expected_down_frames=[InputAudioRawFrame])
    assert not any(down[0].audio)
    await asyncio.sleep(0.25)
    down, _ = await run_test(g, frames_to_send=[loud], expected_down_frames=[InputAudioRawFrame])
    assert down[0].audio == loud.audio
    print("mic gate OK")


class FakeGpo:
    def __init__(self, ok=True): self.writes, self.ok = [], ok
    def write(self, pin, on): self.writes.append((pin, on)); return self.ok


async def test_crystal_ball():
    from pipecat.frames.frames import TTSTextFrame
    gpo = FakeGpo()
    ball = CrystalBall(Light(gpo, 11, "Crystal ball"), max_on_secs=0.3)
    # the safety filter lets the cues through; the ball strips them before the TTS
    f = SentenceSafetyFilter("FALLBACK.", allowed_tags=BALL_CUES)
    down, _ = await run_test(f, frames_to_send=[
        LLMTextFrame("Hee hee! Let me look. [Crystal Ball On] I see sprouts in your fut"),
        LLMTextFrame("ure! [crystal ball off] Now, your costume?"), LLMFullResponseEndFrame()])
    assert texts(down) == ("Hee hee! Let me look. [crystal ball on] I see sprouts in your future! "
                           "[crystal ball off] Now, your costume? "), texts(down)
    down, _ = await run_test(ball.cues, frames_to_send=[f for f in down if isinstance(f, LLMTextFrame)])
    assert texts(down) == "Hee hee! Let me look. I see sprouts in your future! Now, your costume? ", texts(down)
    assert gpo.writes == []  # nothing until she actually says it

    async def play(word):  # the speaker reaching each word, as ElevenLabs' timings say
        await ball.playback.process_frame(TTSTextFrame(word, aggregated_by="word"), FrameDirection.DOWNSTREAM)

    for w in "Hee hee! Let me look.".split():
        await play(w)
    assert gpo.writes == []
    await play("I")  # first word after the cue
    assert gpo.writes == [(11, True)]
    for w in "see sprouts in your future!".split():
        await play(w)
    assert gpo.writes == [(11, True)]
    await play("Now,")
    assert gpo.writes == [(11, True), (11, False)]
    await ball.playback.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    # a cue at the very end waits for her to stop talking
    await run_test(ball.cues, frames_to_send=[LLMTextFrame("[crystal ball on] Ooh! "), LLMTextFrame("[crystal ball off]")])
    await play("Ooh!")
    assert ball.is_on
    await ball.playback.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert not ball.is_on
    # words the TTS never reports still can't strand a cue: stopping applies it
    await run_test(ball.cues, frames_to_send=[LLMTextFrame("Look! [crystal ball on] Ooh. ")])
    await ball.playback.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert ball.is_on
    # forgotten: goes off by itself
    await asyncio.sleep(0.4)
    assert not ball.is_on and gpo.writes[-1] == (11, False)
    # no hardware: nothing raises, and state stays unknown so it retries
    dead = Light(FakeGpo(ok=False), 39, "Eyes")
    await dead.set(True)
    assert dead.is_on is None
    print("crystal ball OK")


async def test_eyes():
    from datetime import datetime
    assert parse_hours("8-17") == (8, 17)
    assert in_hours((8, 17), datetime(2026, 10, 15, 16, 59)) and not in_hours((8, 17), datetime(2026, 10, 15, 17, 0))
    gpo = FakeGpo()
    eyes, ball = Light(gpo, 39, "Eyes"), CrystalBall(Light(gpo, 11, "Crystal ball"))
    task, gate = FakeTask(), MicGate()
    c = PresenceController(task, gate, engage_dist=250, leave_secs=0.2, max_session_secs=60,
                           eyes=eyes, eyes_hours=(0, 24), ball=ball)
    await c._tick()
    assert eyes.is_on is False
    await c._on_presence(json.dumps({"count": 1, "nearest": 900}).encode())  # far away still counts
    await c._tick()
    assert eyes.is_on and c.state == State.IDLE
    await ball.turn_on()
    await c._on_presence(b'{"count": 1}'); await c._tick()
    await asyncio.sleep(0.3); await c._tick()
    assert c.state == State.IDLE and eyes.is_on is False and not ball.is_on
    c._eyes_hours = (0, 0)  # night: never on
    await c._on_presence(b'{"count": 1}'); await c._tick()
    assert eyes.is_on is False
    # pinned by an admin command: the camera no longer switches them
    await eyes.force(True); await c._tick()  # still night
    assert eyes.is_on
    c._eyes_hours = (0, 24)
    await eyes.force(False); await c._tick()  # someone's in view
    assert eyes.is_on is False
    await eyes.force(None); await c._tick()
    assert eyes.is_on
    print("eyes OK")


async def test_admin():
    assert split_magic("Sudo, reset.") == "reset" and split_magic("Pseudo crystal-ball ON") == "crystal ball on"
    assert split_magic("sudo") == "" and split_magic("I'm a sudoku fan") is None
    gpo = FakeGpo()
    eyes, ball = Light(gpo, 39, "Eyes"), CrystalBall(Light(gpo, 11, "Crystal ball"), max_on_secs=0.1)
    task, gate = FakeTask(), MicGate()
    admin = AdminCommands(eyes, ball)
    c = PresenceController(task, gate, engage_dist=250, leave_secs=5, max_session_secs=0,
                           eyes=eyes, eyes_hours=(0, 0), ball=ball, admin=admin)
    admin.reset = c.reset

    def said(text):
        return TranscriptionFrame(text, "user", "now")

    # commands are swallowed; ordinary speech goes through
    down, _ = await run_test(admin, frames_to_send=[
        said("Sudo eyes on."), said("sudo crystal ball on"), said("Are you a witch?"),
        InterimTranscriptionFrame("sudo eyes", "user", "now")])
    assert [f.text for f in down if isinstance(f, (TranscriptionFrame, InterimTranscriptionFrame))] == ["Are you a witch?"]
    assert eyes.is_on and eyes.forced and ball.is_on
    await asyncio.sleep(0.15)
    assert ball.is_on  # held: no auto-off
    # a pause after the magic word: the next transcript is the command
    down, _ = await run_test(admin, frames_to_send=[said("Sudo."), said("Eyes off."), said("sudo crystal ball off")])
    assert not any(isinstance(f, TranscriptionFrame) for f in down)
    assert eyes.is_on is False and not ball.is_on
    # session limit: farewell, mic stays open for commands only
    await c._on_presence(b'{"count": 2}'); await c._tick()
    assert c.state == State.COOLDOWN and gate.is_open and admin.commands_only
    down, _ = await run_test(admin, frames_to_send=[said("Bye witch!")])
    assert not any(isinstance(f, TranscriptionFrame) for f in down)
    # sudo reset: fresh chat, as if they'd just arrived
    n = len(task.queued)
    c._max_session = 60
    await run_test(admin, frames_to_send=[said("sudo reset")])
    assert c.state == State.ENGAGED and not admin.commands_only and gate.is_open
    f = task.queued[n]
    assert isinstance(f, LLMMessagesUpdateFrame) and f.run_llm and "group of 2" in f.messages[0]["content"]
    print("admin OK")


async def test_stale_context():
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", "x")
    import bot
    from pipecat.processors.aggregators.llm_context import LLMContext
    llm = bot.WitchLLM(api_key="x")
    await llm._process_context(LLMContext())  # cleared conversation: no request, no error
    # controller frames queued before the pipeline starts are delivered once it has
    from demon.presence import FrameInlet, greeting
    inlet = FrameInlet()
    await inlet.queue_frames([greeting(1)])
    down, _ = await run_test(inlet, frames_to_send=[], expected_down_frames=[LLMMessagesUpdateFrame])
    print("stale context OK")


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
    await test_crystal_ball(); await test_eyes(); await test_admin(); await test_stale_context()
    test_situation()
    print("ALL PASSED")

asyncio.run(main())
