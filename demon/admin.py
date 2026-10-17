"""Spoken admin commands, for the puppeteer standing in the garden.

Anything that starts with the magic word ("sudo") is taken off the
transcript before the witch can hear it and run as a command instead:

    sudo reset               fresh conversation, as if visitors just walked up
    sudo eyes on|off|auto    pin the eyes on or off; auto hands them back to the camera
    sudo crystal ball on|off switch the ball (on stays on until turned off or visitors leave)

Deepgram may send "sudo" and the command as separate transcripts if there's a
pause between them, so a bare "sudo" arms the next transcript for ARM_SECS.

In commands_only mode (set by the presence controller after her farewell),
everything that isn't a command is dropped too, so the mic can stay open for
"sudo reset" without the visitors getting another reply.
"""

import re
import time
from typing import Awaitable, Callable

from loguru import logger

from pipecat.frames.frames import Frame, InterimTranscriptionFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .lights import CrystalBall, Light

# How speech-to-text tends to spell "sudo".
MAGIC_WORDS = ("sudo", "pseudo", "sue do", "sue doe", "su do")

_ON_OFF = {"on": True, "off": False, "auto": None}
_EYES_RE = re.compile(r"^(?:eyes?|ice|ayes) (on|off|auto)$")
_BALL_RE = re.compile(r"^(?:(?:crystal|crystals) )?ball (on|off)$")


def normalise(text: str) -> str:
    """'Sudo, Crystal-ball ON.' -> 'sudo crystal ball on'"""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def split_magic(text: str) -> str | None:
    """The command after the magic word ('' for the magic word alone), or None if it's not there."""
    text = normalise(text)
    for word in MAGIC_WORDS:
        if text == word or text.startswith(word + " "):
            return text[len(word):].strip()
    return None


class AdminCommands(FrameProcessor):
    """Sits between the STT and the user aggregator."""

    ARM_SECS = 5.0

    def __init__(self, eyes: Light, ball: CrystalBall,
                 reset: Callable[[], Awaitable[None]] | None = None, **kwargs):
        super().__init__(**kwargs)
        self._eyes = eyes
        self._ball = ball
        self.reset = reset  # set once the presence controller exists
        self.commands_only = False
        self._armed_until = 0.0

    def _armed(self) -> bool:
        return time.monotonic() < self._armed_until

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            command = split_magic(frame.text)
            if command is None and self._armed():
                command = normalise(frame.text)
            if command == "":
                logger.info("Admin: magic word heard, waiting for the command")
                self._armed_until = time.monotonic() + self.ARM_SECS
                return
            if command is not None:
                self._armed_until = 0.0
                await self._run(command)
                return
            if self.commands_only:
                logger.info("Ignoring speech after farewell (commands only)")
                return
        elif isinstance(frame, InterimTranscriptionFrame):
            if self.commands_only or self._armed() or split_magic(frame.text) is not None:
                return
        await self.push_frame(frame, direction)

    async def _run(self, command: str):
        if command in ("reset", "restart"):
            logger.info("Admin: reset")
            if self.reset:
                await self.reset()
        elif m := _EYES_RE.match(command):
            logger.info(f"Admin: eyes {m.group(1)}")
            await self._eyes.force(_ON_OFF[m.group(1)])
        elif m := _BALL_RE.match(command):
            logger.info(f"Admin: crystal ball {m.group(1)}")
            if m.group(1) == "on":
                await self._ball.turn_on(hold=True)
            else:
                await self._ball.turn_off()
        else:
            logger.warning(f"Admin: unknown command {command!r}")
