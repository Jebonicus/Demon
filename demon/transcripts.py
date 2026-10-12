"""Debug logging for the conversation: what the STT heard, what the LLM said, what the TTS played."""

import time

from loguru import logger

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class TranscriptLogger(FrameProcessor):
    """Logs everything the speech-to-text service hears. Sits right after the STT service."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            logger.info(f"STT heard: {frame.text!r}")
        elif isinstance(frame, InterimTranscriptionFrame):
            logger.info(f"STT heard (interim): {frame.text!r}")
        await self.push_frame(frame, direction)


class LLMResponseLogger(FrameProcessor):
    """Logs each complete LLM reply, and whether it was cut short. Sits right after the LLM."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._text = None  # None = no response in progress
        self._started = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self._text, self._started = "", time.monotonic()
            logger.info("LLM response started")
        elif isinstance(frame, LLMTextFrame) and self._text is not None:
            self._text += frame.text
        elif isinstance(frame, LLMFullResponseEndFrame) and self._text is not None:
            secs = time.monotonic() - self._started
            if self._text.strip():
                logger.info(f"LLM replied ({secs:.1f}s): {self._text.strip()!r}")
            else:
                logger.warning(f"LLM response was empty ({secs:.1f}s)")
            self._text = None
        elif isinstance(frame, InterruptionFrame) and self._text is not None:
            logger.info(f"LLM response interrupted after {self._text.strip()!r}")
            self._text = None
        elif isinstance(frame, ErrorFrame):
            logger.error(f"Pipeline error: {frame.error}")
        await self.push_frame(frame, direction)


class TTSLogger(FrameProcessor):
    """Logs when the TTS starts and stops producing audio. Sits right after the TTS."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._audio_secs = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSStartedFrame):
            self._audio_secs = 0.0
            logger.info("TTS started")
        elif isinstance(frame, TTSAudioRawFrame):
            self._audio_secs += len(frame.audio) / (2 * frame.num_channels * frame.sample_rate)
        elif isinstance(frame, TTSStoppedFrame):
            logger.info(f"TTS finished: {self._audio_secs:.1f}s of audio")
        elif isinstance(frame, InterruptionFrame):
            logger.info("Interrupted: discarding any reply audio not yet played")
        elif isinstance(frame, ErrorFrame):
            logger.error(f"Pipeline error: {frame.error}")
        await self.push_frame(frame, direction)
