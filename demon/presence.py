"""Camera-driven conversation control (no wake word).

The JebCam vision Pi publishes who's in front of the house over MQTT. This
module turns that into conversation events:

  IDLE ──(someone within ENGAGE_DIST)────▶ ENGAGED  (reset chat, mic open, greet)
  ENGAGED ──(nobody for LEAVE_SECS)──────▶ IDLE     (reset chat, mic closed)
  ENGAGED ──(MAX_SESSION_SECS elapsed)───▶ COOLDOWN (say farewell, mic closed)
  COOLDOWN ──(nobody for LEAVE_SECS)─────▶ IDLE

Expected MQTT payload on PRESENCE_TOPIC (JSON), published whenever it
changes or at ~1 Hz:
    {"count": 2, "nearest": 180}
"count" is people currently tracked; "nearest" is the nearest person's
distance from the figure, in whatever units the publisher uses (JebCam's
tracker works in pixels from its reference point, so ENGAGE_DIST is in
pixels too). Omit "nearest" to engage on count alone.

The mic is gated closed while IDLE, so you don't pay to stream the street
to the speech-to-text service all evening, and the demon can't be woken by
a conversation on the pavement.
"""

import asyncio
import json
import time
from enum import Enum

from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    LLMMessagesAppendFrame,
    LLMMessagesUpdateFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from . import persona


class State(Enum):
    IDLE = "idle"
    ENGAGED = "engaged"
    COOLDOWN = "cooldown"


class MicGate(FrameProcessor):
    """Drops mic audio unless the gate is open. Sits right after transport.input().

    With mute_while_speaking, it also swaps the mic for silence while the bot
    talks and for tail_secs after, so speech-to-text never hears her own voice.
    The user aggregator's mute isn't enough on its own: it sits after STT, and
    Deepgram's transcript of her last words arrives after she has stopped, by
    which point that mute has lifted and her words count as the visitor's.
    Silence rather than nothing, so Deepgram wraps up any half-heard words
    while still muted instead of finishing them late.
    """

    def __init__(self, open_: bool = False, mute_while_speaking: bool = False,
                 tail_secs: float = 0.5, **kwargs):
        super().__init__(**kwargs)
        self.is_open = open_
        self._mute_while_speaking = mute_while_speaking
        self._tail_secs = tail_secs
        self._bot_speaking = False
        self._muted_until = 0.0

    def _hearing_bot(self) -> bool:
        return self._mute_while_speaking and (
            self._bot_speaking or time.monotonic() < self._muted_until)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._muted_until = time.monotonic() + self._tail_secs
        elif isinstance(frame, InputAudioRawFrame):
            if not self.is_open:
                return
            if self._hearing_bot():
                frame = InputAudioRawFrame(audio=bytes(len(frame.audio)),
                                           sample_rate=frame.sample_rate,
                                           num_channels=frame.num_channels)
        await self.push_frame(frame, direction)


class PresenceController:
    # A new head count must hold this long before she's told about it, so a
    # tracker flickering between 2 and 3 doesn't send her a stream of updates.
    COUNT_SETTLE_SECS = 3.0

    def __init__(self, task, gate: MicGate, *, engage_dist: float, leave_secs: float,
                 max_session_secs: float):
        self._task = task
        self._gate = gate
        self._engage_dist = engage_dist
        self._leave_secs = leave_secs
        self._max_session = max_session_secs
        self.state = State.IDLE
        self._last_seen = 0.0
        self._session_start = 0.0
        self._told_count = 0
        self._new_count = 0
        self._new_count_since = 0.0
        self._events: asyncio.Queue = asyncio.Queue()

    # --- called from the MQTT thread ------------------------------------
    def feed_threadsafe(self, loop: asyncio.AbstractEventLoop, payload: bytes):
        loop.call_soon_threadsafe(self._events.put_nowait, payload)

    # --- asyncio side ------------------------------------------------------
    async def run(self):
        while True:
            try:
                payload = await asyncio.wait_for(self._events.get(), timeout=1.0)
                await self._on_presence(payload)
            except asyncio.TimeoutError:
                pass
            await self._tick()

    async def _on_presence(self, payload: bytes):
        try:
            msg = json.loads(payload)
            count = int(msg.get("count", 0))
            nearest = msg.get("nearest")
        except (ValueError, TypeError, AttributeError):
            logger.warning(f"Bad presence payload: {payload!r}")
            return

        if count > 0:
            self._last_seen = time.monotonic()

        close_enough = count > 0 and (nearest is None or float(nearest) <= self._engage_dist)
        if self.state == State.IDLE and close_enough:
            await self._engage(count)
        elif self.state == State.ENGAGED and count > 0:
            await self._track_count(count)

    async def _track_count(self, count: int):
        now = time.monotonic()
        if count == self._told_count:
            self._new_count = 0
            return
        if count != self._new_count:
            self._new_count, self._new_count_since = count, now
            return
        if now - self._new_count_since >= self.COUNT_SETTLE_SECS:
            logger.info(f"Head count now {count}")
            self._told_count, self._new_count = count, 0
            await self._task.queue_frames([LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": persona.headcount_direction(count)}],
                run_llm=False,
            )])

    async def _tick(self):
        now = time.monotonic()
        gone = now - self._last_seen > self._leave_secs

        if self.state == State.ENGAGED:
            if gone:
                await self._disengage("visitors left")
            elif now - self._session_start > self._max_session:
                logger.info("Session time limit reached — saying farewell")
                self.state = State.COOLDOWN
                await self._task.queue_frames([
                    LLMMessagesAppendFrame(
                        messages=[{"role": "user", "content": persona.WRAP_UP_DIRECTION}],
                        run_llm=True,
                    )
                ])
                # Close the mic after the farewell has had time to play.
                asyncio.get_running_loop().call_later(6.0, self._close_gate_if_cooldown)
        elif self.state == State.COOLDOWN and gone:
            await self._disengage("cooldown over")

    def _close_gate_if_cooldown(self):
        if self.state == State.COOLDOWN:
            self._gate.is_open = False

    async def _engage(self, count: int):
        logger.info(f"Engaging: {count} visitor(s)")
        self.state = State.ENGAGED
        self._session_start = time.monotonic()
        self._told_count, self._new_count = count, 0
        self._gate.is_open = True
        await self._task.queue_frames([
            LLMMessagesUpdateFrame(
                messages=[{"role": "user", "content": persona.arrival_direction(count)}],
                run_llm=True,
            )
        ])

    async def _disengage(self, why: str):
        logger.info(f"Back to idle ({why}); clearing conversation")
        self.state = State.IDLE
        self._gate.is_open = False
        await self._task.queue_frames([LLMMessagesUpdateFrame(messages=[], run_llm=False)])


def start_mqtt(controller: PresenceController, loop, host: str, port: int, topic: str,
               username: str | None = None, password: str | None = None):
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="demon-robot")
    if username:
        client.username_pw_set(username, password)

    def on_connect(c, userdata, flags, reason_code, properties):
        logger.info(f"MQTT connected ({reason_code}); subscribing to {topic}")
        c.subscribe(topic)

    def on_message(c, userdata, msg):
        controller.feed_threadsafe(loop, msg.payload)

    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(host, port, keepalive=30)
    client.loop_start()
    return client
