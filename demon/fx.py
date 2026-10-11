"""Demonic voice effects applied to the TTS audio before it's played.

Runs before the speaker output, so the XVF3800's echo canceller gets the
*processed* audio as its reference, as it should.

Pitch shifting uses a classic two-tap "delay-line" (Doppler) shifter, the
trick old studio harmonizers used. It adds only ~25 ms of delay, versus about
a full second for pedalboard's high-quality PitchShift in streaming mode,
and its slightly gritty, warbly character suits a demon. The remaining
effects come from Spotify's pedalboard, run in streaming mode (reset=False)
so reverb tails carry across chunks; none of those add delay.

FX_PRESET options:
  off    - passthrough
  lite   - effects but no pitch shift (cheapest)
  demon  - pitch down + growl + small reverb (default)
"""

import numpy as np
from loguru import logger

from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

try:
    import pedalboard as pb
except ImportError:  # keeps the bot runnable without FX
    pb = None


class DopplerPitchShifter:
    """Streaming, low-latency pitch shifter (mono float32).

    Two read taps sweep through a short delay line at a rate set by the pitch
    ratio, cross-faded with sin^2 windows that always sum to 1, so the jump
    when each tap wraps around is silent.
    """

    def __init__(self, semitones: float, sample_rate: int, window_ms: float = 50.0):
        self.ratio = 2.0 ** (semitones / 12.0)
        self.W = max(64, int(sample_rate * window_ms / 1000.0))
        self.H = self.W + 2
        self.reset()

    def reset(self):
        self.hist = np.zeros(self.H, dtype=np.float32)
        self.phase = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        if n == 0:
            return x
        ext = np.concatenate([self.hist, x])
        k = np.arange(n)
        step = (1.0 - self.ratio) / self.W
        p1 = (self.phase + k * step) % 1.0
        out = np.zeros(n, dtype=np.float32)
        for p in (p1, (p1 + 0.5) % 1.0):
            pos = (self.H + k) - p * self.W            # fractional read position
            i0 = np.floor(pos).astype(np.int64)
            frac = (pos - i0).astype(np.float32)
            i1 = np.minimum(i0 + 1, self.H + k)
            tap = ext[i0] * (1.0 - frac) + ext[i1] * frac
            out += tap * (np.sin(np.pi * p) ** 2).astype(np.float32)
        self.phase = (self.phase + n * step) % 1.0
        self.hist = ext[-self.H:].copy()
        return out


def build_board(highpass_hz: float):
    if pb is None:
        return None
    return pb.Pedalboard([
        # Cuts the boom that carries through walls (and that a tiny speaker can't play anyway).
        pb.HighpassFilter(cutoff_frequency_hz=highpass_hz),
        # A touch of chorus + distortion = "two voices at once" growl.
        pb.Chorus(rate_hz=0.8, depth=0.15, centre_delay_ms=12, feedback=0.0, mix=0.35),
        pb.Distortion(drive_db=6),
        pb.Reverb(room_size=0.25, damping=0.6, wet_level=0.12, dry_level=0.9, width=0.0),
        pb.Gain(gain_db=-3),
        pb.Limiter(threshold_db=-1.0, release_ms=80),
    ])


class DemonVoiceFX(FrameProcessor):
    def __init__(self, preset: str = "demon", semitones: float = -4.0,
                 highpass_hz: float = 180.0, flush_secs: float = 0.35, **kwargs):
        super().__init__(**kwargs)
        self._enabled = preset != "off"
        self._use_pitch = preset == "demon" and semitones != 0
        self._semitones = semitones
        self._board = build_board(highpass_hz) if self._enabled else None
        self._shifter = None
        self._flush_secs = flush_secs
        self._sample_rate = None
        if self._enabled and pb is None:
            logger.warning("pedalboard not installed: only pitch shift will be applied")
        logger.info(f"Voice FX preset={preset} semitones={semitones if self._use_pitch else 0}")

    def _reset(self):
        if self._board is not None:
            self._board.reset()
        if self._shifter is not None:
            self._shifter.reset()

    def process_chunk(self, pcm: bytes, sample_rate: int) -> bytes:
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if self._use_pitch:
            if self._shifter is None or self._sample_rate != sample_rate:
                self._shifter = DopplerPitchShifter(self._semitones, sample_rate)
            x = self._shifter.process(x)
        self._sample_rate = sample_rate
        if self._board is not None:
            x = self._board(x.reshape(1, -1), sample_rate, reset=False).reshape(-1)
        x = np.clip(x, -1.0, 1.0)
        return (x * 32767.0).astype(np.int16).tobytes()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if self._enabled:
            if isinstance(frame, TTSAudioRawFrame) and frame.num_channels == 1:
                frame = TTSAudioRawFrame(
                    audio=self.process_chunk(frame.audio, frame.sample_rate),
                    sample_rate=frame.sample_rate,
                    num_channels=1,
                )
            elif isinstance(frame, TTSStoppedFrame) and self._sample_rate:
                # Push a little silence through so the reverb tail plays out
                # instead of being chopped off.
                silence = bytes(int(self._sample_rate * self._flush_secs) * 2)
                await self.push_frame(TTSAudioRawFrame(
                    audio=self.process_chunk(silence, self._sample_rate),
                    sample_rate=self._sample_rate, num_channels=1,
                ))
                self._reset()
            elif isinstance(frame, InterruptionFrame):
                self._reset()

        await self.push_frame(frame, direction)
