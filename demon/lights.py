"""Lights wired to the XVF3800's GPO pins: the crystal ball and the eyes.

Pins are switched with the same USB vendor request as the ReSpeaker tool:
    python xvf_host.py GPO_WRITE_VALUE --values 11 1     (0 = off)

The crystal ball is driven by the witch herself: her replies carry silent
[crystal ball on] / [crystal ball off] cues. The LLM writes the whole reply
long before she has finished saying it, so the cues are timed against the
audio instead: CrystalBall.cues (before the TTS) strips each cue and notes
how many letters of speech come before it; CrystalBall.playback (after the
speaker output) counts the letters of each word as ElevenLabs' word timings
say it's played, and switches the ball when the next word after the cue
starts. Whenever she stops talking, any cues still waiting are applied and
the counts re-synced, so a miscount can't build up. If she forgets to turn
it off, it goes off by itself after max_on_secs.

A missing or unplugged XVF3800 is logged once and otherwise ignored: the
lights are decoration, not worth crashing the conversation over.
"""

import asyncio
import re
import threading
from collections import deque
from datetime import datetime

from loguru import logger

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    LLMTextFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

XVF_VID = 0x2886
GPO_RESID = 20             # GPO_SERVICER_RESID in xvf_host.py
GPO_WRITE_VALUE_CMDID = 1  # payload: [pin, value]

CRYSTAL_BALL_PIN = 11
EYES_PIN = 39
BALL_CUES = ("crystal ball on", "crystal ball off")


class XVFGpo:
    """Writes the XVF3800's GPO pins over USB (blocking; call via Light)."""

    TIMEOUT_MS = 1000

    def __init__(self, vid: int = XVF_VID):
        self._vid = vid
        self._dev = None
        self._lock = threading.Lock()
        self._warned = False

    def _device(self):
        if self._dev is None:
            import usb.core
            devices = list(usb.core.find(find_all=True, idVendor=self._vid) or [])
            if not devices:
                raise RuntimeError(f"no USB device with VID 0x{self._vid:04x}")
            # Same choice as xvf_host.py when no PID is given.
            self._dev = min(devices, key=lambda d: getattr(d, "idProduct", 0))
        return self._dev

    def write(self, pin: int, on: bool) -> bool:
        import usb.util
        with self._lock:
            try:
                self._device().ctrl_transfer(
                    usb.util.CTRL_OUT | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
                    0, GPO_WRITE_VALUE_CMDID, GPO_RESID, bytes([pin, int(on)]), self.TIMEOUT_MS)
            except Exception as e:
                self._dev = None  # re-find next time, in case it was replugged
                if not self._warned:
                    logger.warning(f"Can't switch XVF3800 GPO pin {pin}: {e} (lights disabled until it works)")
                    self._warned = True
                return False
            if self._warned:
                logger.info("XVF3800 GPO pins working again")
                self._warned = False
            return True


class Light:
    """One light on one GPO pin. Only writes when the state actually changes."""

    def __init__(self, gpo: XVFGpo, pin: int, name: str):
        self._gpo = gpo
        self.pin = pin
        self.name = name
        self.is_on = None  # unknown until first written
        self.forced: bool | None = None  # pinned on/off by an admin command; None = automatic

    async def force(self, on: bool | None):
        """Pin the light on or off, whatever automatic control wants; None hands it back."""
        self.forced = on
        if on is not None:
            await self.set(on)

    async def set(self, on: bool):
        if on == self.is_on:
            return
        if await asyncio.to_thread(self._gpo.write, self.pin, on):
            logger.info(f"{self.name} {'on' if on else 'off'}")
            self.is_on = on


def in_hours(hours: tuple[int, int], now: datetime | None = None) -> bool:
    """True if now falls in [start, end) hours, e.g. (8, 17) is 08:00 to 16:59."""
    start, end = hours
    return start <= (now or datetime.now()).hour < end


def parse_hours(spec: str) -> tuple[int, int]:
    """'8-17' -> (8, 17)."""
    start, end = spec.split("-")
    return int(start), int(end)


_CUE_RE = re.compile(r"\[crystal ball (on|off)\]")


class _LetterCounter:
    """Counts letters and digits, skipping [audio tags] even when split across frames."""

    def __init__(self):
        self.in_tag = False

    def count(self, text: str) -> int:
        n = 0
        for c in text:
            if c == "[":
                self.in_tag = True
            elif c == "]":
                self.in_tag = False
            elif not self.in_tag and c.isalnum():
                n += 1
        return n


class CrystalBall:
    """Turns the witch's [crystal ball on/off] cues into the light, in time with her voice.

    Put `cues` between the safety filter (which must let the cues through) and the
    TTS, and `playback` straight after transport.output().
    """

    def __init__(self, light: Light, max_on_secs: float = 40.0):
        self._light = light
        self._max_on = max_on_secs
        self._auto_off: asyncio.Task | None = None
        self._sent = 0    # letters passed to the TTS so far
        self._played = 0  # letters the speaker has reached so far
        self._pending: deque[tuple[int, bool]] = deque()  # (letter position, on)
        self.cues = _CueReader(self)
        self.playback = _PlaybackWatcher(self)

    @property
    def is_on(self) -> bool:
        return bool(self._light.is_on)

    async def turn_on(self, hold: bool = False):
        """hold: skip the max_on_secs auto-off (for an admin command)."""
        if self._auto_off:
            self._auto_off.cancel()
            self._auto_off = None
        if not hold:
            self._auto_off = asyncio.create_task(self._off_later())
        await self._light.set(True)

    async def turn_off(self):
        self._pending.clear()
        if self._auto_off:
            self._auto_off.cancel()
            self._auto_off = None
        await self._light.set(False)

    async def _off_later(self):
        await asyncio.sleep(self._max_on)
        self._auto_off = None
        if self._light.is_on:
            logger.info("Crystal ball left on too long: turning it off")
            await self.turn_off()

    async def _apply(self, on: bool):
        if on:
            await self.turn_on()
        else:
            await self.turn_off()

    async def _played_up_to(self, letters: int):
        """The speaker has started the word that ends at this letter: apply the cues before it."""
        self._played = letters
        while self._pending and self._pending[0][0] < letters:
            await self._apply(self._pending.popleft()[1])

    async def _caught_up(self):
        """She's stopped talking: apply whatever's left and re-sync the counts."""
        if self._pending and self._pending[0][0] < self._sent:
            # Expected for a cue at the very end of a reply; anything earlier
            # means the word timings didn't match the text sent.
            logger.info(f"Crystal ball cue applied at end of speech "
                        f"(heard {self._played} of {self._sent} letters)")
        while self._pending:
            await self._apply(self._pending.popleft()[1])
        self._played = self._sent


class _CueReader(FrameProcessor):
    def __init__(self, ball: CrystalBall, **kwargs):
        super().__init__(**kwargs)
        self._ball = ball

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame) and _CUE_RE.search(frame.text):
            parts = _CUE_RE.split(frame.text)  # text, cue, text, cue, ..., text
            counter = _LetterCounter()
            for i, part in enumerate(parts):
                if i % 2:
                    self._ball._pending.append((self._ball._sent, part == "on"))
                else:
                    self._ball._sent += counter.count(part)
            text = re.sub(r"\s+", " ", _CUE_RE.sub(" ", frame.text)).strip()
            if not text:
                return
            frame = LLMTextFrame(text=text + " ")
        elif isinstance(frame, LLMTextFrame):
            self._ball._sent += _LetterCounter().count(frame.text)
        await self.push_frame(frame, direction)


class _PlaybackWatcher(FrameProcessor):
    def __init__(self, ball: CrystalBall, **kwargs):
        super().__init__(**kwargs)
        self._ball = ball
        self._counter = _LetterCounter()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSTextFrame):
            n = self._counter.count(frame.text)
            if n:
                await self._ball._played_up_to(self._ball._played + n)
        elif isinstance(frame, (BotStoppedSpeakingFrame, InterruptionFrame)):
            self._counter = _LetterCounter()
            await self._ball._caught_up()
        await self.push_frame(frame, direction)
