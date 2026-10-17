#!/usr/bin/env python3
"""Talking demon for the front garden.

Pipeline:
  mic ─▶ MicGate ─▶ Deepgram STT ─▶ context ─▶ Claude ─▶ safety filter
      ─▶ crystal ball cues ─▶ ElevenLabs TTS ─▶ demon FX ─▶ speaker

Run on a laptop for testing (always listening):
    python bot.py --always-present

Run on the Pi (camera-triggered via MQTT):
    python bot.py
"""

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import LLMMessagesUpdateFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.workers.runner import WorkerRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.dialogue.tts import ElevenLabsDialogueTTSService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.turns.user_mute import AlwaysUserMuteStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from demon import persona
from demon.fx import DemonVoiceFX
from demon.lights import (BALL_CUES, CRYSTAL_BALL_PIN, EYES_PIN, CrystalBall, Light, XVFGpo,
                          parse_hours)
from demon.presence import MicGate, PresenceController, start_mqtt
from demon.safety import SentenceSafetyFilter
from demon.transcripts import (LLMResponseLogger, TranscriptLogger, TTSLogger,
                               add_transcript_syslog, transcript)


def env(name, default=None, cast=str):
    v = os.getenv(name)
    return default if v in (None, "") else cast(v)


def parse_bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def list_audio_devices():
    import pyaudio
    pa = pyaudio.PyAudio()
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        print(f"{i:3d}  in={d['maxInputChannels']} out={d['maxOutputChannels']}  "
              f"{int(d['defaultSampleRate'])} Hz  {d['name']}")
    pa.terminate()


def build_transport(sample_rate: int):
    from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

    return LocalAudioTransport(LocalAudioTransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=sample_rate,
        audio_out_sample_rate=sample_rate,
        input_device_index=env("AUDIO_IN_DEVICE", None, int),
        output_device_index=env("AUDIO_OUT_DEVICE", None, int),
        # Default is 4 (40 ms), too little slack on a Pi 3: playback underruns and crackles.
        audio_out_10ms_chunks=env("AUDIO_OUT_CHUNKS", 10, int),
    ))


def uses_dialogue_api(model: str) -> bool:
    """v3/v4 models are only reachable through ElevenLabs' Text-to-Dialogue API."""
    return model.startswith(("eleven_v3", "eleven_v4"))


def build_tts(sample_rate: int):
    model = env("ELEVENLABS_MODEL", "eleven_flash_v2_5")
    voice = os.environ["ELEVENLABS_VOICE_ID"]
    if uses_dialogue_api(model):
        # Text-to-Dialogue only reads `stability` (lower = more expressive).
        # Pipecat 1.12 logs "Text-to-Dialogue requires an eleven_v3 model" for v4;
        # that check predates v4 and is only a warning: the model is passed through.
        return ElevenLabsDialogueTTSService(
            api_key=os.environ["ELEVENLABS_API_KEY"],
            sample_rate=sample_rate,
            settings=ElevenLabsDialogueTTSService.Settings(
                voice=voice,
                model=model,
                stability=env("VOICE_STABILITY", 0.45, float),
            ),
        )
    return ElevenLabsTTSService(
        api_key=os.environ["ELEVENLABS_API_KEY"],
        sample_rate=sample_rate,
        settings=ElevenLabsTTSService.Settings(
            voice=voice,
            model=model,
            stability=env("VOICE_STABILITY", 0.45, float),
            similarity_boost=env("VOICE_SIMILARITY", 0.8, float),
            style=env("VOICE_STYLE", 0.35, float),
        ),
    )


def build_task(transport, sample_rate: int, gate: MicGate, ball: CrystalBall) -> PipelineWorker:
    audio_tags = uses_dialogue_api(env("ELEVENLABS_MODEL", "eleven_flash_v2_5"))
    stt = DeepgramSTTService(
        api_key=os.environ["DEEPGRAM_API_KEY"],
        settings=DeepgramSTTService.Settings(
            language=env("STT_LANGUAGE", "en-GB"),
            keyterm=list(persona.STT_KEYTERMS),
        ),
    )
    llm = AnthropicLLMService(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        settings=AnthropicLLMService.Settings(
            model=env("LLM_MODEL", "claude-haiku-5-5"),
            system_instruction=persona.system_prompt(audio_tags=audio_tags),
            max_tokens=150,  # hard cap on rambling
            # Haiku 5.5 thinks adaptively unless told not to, and thinking tokens count
            # against max_tokens: replies came back empty or cut off mid-word.
            thinking=AnthropicLLMService.ThinkingConfig(type="disabled"),
            # (no temperature: current Claude models reject it)
        ),
    )
    tts = build_tts(sample_rate)

    context = LLMContext()
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=VADParams(
                confidence=env("VAD_CONFIDENCE", 0.7, float),
                stop_secs=env("VAD_STOP_SECS", 0.2, float),
                min_volume=env("VAD_MIN_VOLUME", 0.6, float),
            )),
            # Default stop strategy runs the Smart Turn model on every pause, which is
            # heavy for a Pi 3 and can end a turn before Deepgram's transcript arrives.
            # This one waits for the transcript.
            user_turn_strategies=UserTurnStrategies(stop=[SpeechTimeoutUserTurnStopStrategy(
                user_speech_timeout=env("TURN_STOP_SECS", 0.6, float),
            )]),
            # Default answers a transcript-less turn that overlapped the bot by asking
            # the visitor to repeat themselves: the "didn't hear you" replies.
            empty_user_turn=None,
            # Ignore the mic while the bot talks. Otherwise its own voice leaking back
            # into the mic counts as the visitor interrupting, and cuts the reply off.
            user_mute_strategies=(
                [AlwaysUserMuteStrategy()] if env("MUTE_WHILE_SPEAKING", True, parse_bool) else []
            ),
        ),
    )

    @aggregators.user().event_handler("on_user_turn_started")
    async def on_user_turn_started(aggregator, strategy):
        logger.info(f"User turn started ({type(strategy).__name__})")

    @aggregators.user().event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(aggregator, strategy, message):
        if message.content:
            transcript.info(f"Sent to LLM: {message.content!r}")
        else:
            logger.info("User turn ended with no transcript (ignored)")

    pipeline = Pipeline([
        transport.input(),
        gate,
        stt,
        TranscriptLogger(),
        aggregators.user(),
        llm,
        LLMResponseLogger(),
        SentenceSafetyFilter(persona.SAFE_FALLBACK_LINE,
                             allowed_tags=(persona.AUDIO_TAGS if audio_tags else ()) + BALL_CUES),
        ball.cues,  # strips the cues, so it must come before the TTS
        tts,
        TTSLogger(),
        DemonVoiceFX(
            preset=env("FX_PRESET", "demon"),
            semitones=env("FX_SEMITONES", -4.0, float),
            highpass_hz=env("FX_HIGHPASS_HZ", 180.0, float),
        ),
        transport.output(),
        ball.playback,  # sees each word as it's played, to time the ball
        aggregators.assistant(),
    ])

    return PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=sample_rate,
            audio_out_sample_rate=sample_rate,
            enable_metrics=True,
        ),
        idle_timeout_secs=None,  # default would shut the bot down after 5 quiet minutes
    )


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--always-present", action="store_true",
                    help="ignore the camera; listen continuously (laptop testing)")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--env", default=".env")
    args = ap.parse_args()

    load_dotenv(args.env, override=True)
    logger.remove()
    logger.add(sys.stderr, level=env("LOG_LEVEL", "INFO"))
    add_transcript_syslog()

    if args.list_devices:
        list_audio_devices()
        return

    sample_rate = env("SAMPLE_RATE", 16000, int)
    gate = MicGate(open_=args.always_present,
                   mute_while_speaking=env("MUTE_WHILE_SPEAKING", True, parse_bool),
                   tail_secs=env("ECHO_TAIL_SECS", 0.5, float))
    gpo = XVFGpo()
    eyes = Light(gpo, env("EYES_PIN", EYES_PIN, int), "Eyes")
    ball = CrystalBall(Light(gpo, env("CRYSTAL_BALL_PIN", CRYSTAL_BALL_PIN, int), "Crystal ball"),
                       max_on_secs=env("BALL_MAX_SECS", 40.0, float))
    await eyes.set(False)  # known state, whatever a previous run left on
    await ball.turn_off()
    transport = build_transport(sample_rate)
    task = build_task(transport, sample_rate, gate, ball)
    runner = WorkerRunner(handle_sigint=True, handle_sigterm=True)  # sigterm: clean stop under systemd

    side_tasks = []
    if args.always_present:
        await task.queue_frames([LLMMessagesUpdateFrame(
            messages=[{"role": "user", "content": persona.arrival_direction(1)}],
            run_llm=True,
        )])
    else:
        controller = PresenceController(
            task, gate,
            engage_dist=env("ENGAGE_DIST", 250.0, float),
            leave_secs=env("LEAVE_SECS", 8.0, float),
            max_session_secs=env("MAX_SESSION_SECS", 180.0, float),
            eyes=eyes, eyes_hours=env("EYES_HOURS", (7, 18), parse_hours), ball=ball,
        )
        start_mqtt(controller, asyncio.get_running_loop(),
                   host=env("MQTT_HOST", "localhost"), port=env("MQTT_PORT", 1883, int),
                   topic=env("PRESENCE_TOPIC", "halloween/presence"),
                   username=env("MQTT_USER"), password=env("MQTT_PASS"))
        side_tasks.append(asyncio.create_task(controller.run()))

    try:
        await runner.add_workers(task)
        await runner.run()
    finally:
        for t in side_tasks:
            t.cancel()
        await eyes.set(False)
        await ball.turn_off()


if __name__ == "__main__":
    asyncio.run(main())
