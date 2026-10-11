"""Logs everything the speech-to-text service hears. Sits right after the STT service."""

from loguru import logger

from pipecat.frames.frames import Frame, InterimTranscriptionFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class TranscriptLogger(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            logger.info(f"STT heard: {frame.text!r}")
        elif isinstance(frame, InterimTranscriptionFrame):
            logger.info(f"STT heard (interim): {frame.text!r}")
        await self.push_frame(frame, direction)
