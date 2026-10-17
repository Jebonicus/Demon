"""Belt-and-braces output filter between the LLM and the TTS.

The system prompt does the real work; this catches anything that slips
through before it's spoken out loud to a child. It buffers the streamed LLM
text into sentences (the TTS waits for whole sentences anyway, so this adds
no real latency), checks each one, and swaps bad ones for a safe line.
It also strips stage directions like *laughs* or (evil cackle), which the
TTS would otherwise read out literally. With ElevenLabs v3/v4 voices, a short
whitelist of [audio tags] is let through, because those voices act them out.
"""

import re

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Whole-word, case-insensitive. Deliberately broad: a false positive just
# means the demon says its fallback line. Add to this as you test.
BLOCKED_WORDS = [
    # profanity
    "fuck", "fucking", "shit", "bitch", "bastard", "arse", "arsehole", "ass",
    "asshole", "crap", "piss", "dick", "cock", "wanker", "twat", "bollocks",
    "bloody", "damn", "hell", "cunt", 
    # violence / harm
    "kill", "killed", "killing", "murder", "die", "dead", "death", "blood",
    "bleed", "stab", "knife", "gun", "shoot", "suicide", "torture",
    # adult / substances
    "sex", "sexy", "naked", "drunk", "beer", "wine", "vodka", "drugs", "cigarette",
]

_BLOCK_RE = re.compile(r"\b(" + "|".join(map(re.escape, BLOCKED_WORDS)) + r")\b", re.IGNORECASE)
_STAGE_DIR_RE = re.compile(r"\*[^*]*\*|\([^)]*\)|\[[^\]]*\]")
_TAG_RE = re.compile(r"\[([^\]]*)\]")
_SENTENCE_END_RE = re.compile(r"([.!?…]+[\"')\]]*)(\s+|$)")


def clean_sentence(text: str, allowed_tags=()) -> str:
    """Remove stage directions (except allowed [audio tags]) and tidy whitespace."""
    allowed = {t.lower() for t in allowed_tags}
    kept = {}

    def stash(m):  # protect allowed tags from the stripper below
        if m.group(1).strip().lower() in allowed:
            key = f"\x00{len(kept)}\x00"
            kept[key] = f"[{m.group(1).strip().lower()}]"
            return key
        return m.group(0)

    text = _TAG_RE.sub(stash, text)
    text = _STAGE_DIR_RE.sub(" ", text)
    for key, tag in kept.items():
        text = text.replace(key, tag)
    return re.sub(r"\s+", " ", text).strip()


def is_truncated(text: str, allowed_tags=()) -> bool:
    """True for leftover text with words in it but no closing punctuation: a reply
    cut off by max_tokens, which would otherwise be spoken ending mid-word."""
    text = _TAG_RE.sub(" ", clean_sentence(text, allowed_tags)).strip()
    return bool(re.search(r"\w", text)) and not _SENTENCE_END_RE.search(text)


def is_blocked(text: str) -> bool:
    return bool(_BLOCK_RE.search(text))


class SentenceSafetyFilter(FrameProcessor):
    def __init__(self, fallback_line: str, allowed_tags=(), **kwargs):
        super().__init__(**kwargs)
        self._allowed_tags = tuple(allowed_tags)
        self._fallback = fallback_line
        self._buf = ""
        self._tripped = False  # once a response trips, drop the rest of it
        self._spoke = False  # whether this response has emitted a sentence yet

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMTextFrame):
            self._buf += frame.text
            await self._emit_complete_sentences()
            return

        if isinstance(frame, LLMFullResponseEndFrame):
            if self._spoke and is_truncated(self._buf, self._allowed_tags):
                logger.warning(f"Dropping unfinished end of reply: {self._buf.strip()!r}")
            else:
                await self._emit(self._buf)
            self._buf = ""
            self._tripped = False
            self._spoke = False
        elif isinstance(frame, InterruptionFrame):
            self._buf = ""
            self._tripped = False
            self._spoke = False

        await self.push_frame(frame, direction)

    async def _emit_complete_sentences(self):
        while True:
            m = _SENTENCE_END_RE.search(self._buf)
            if not m:
                return
            sentence, self._buf = self._buf[: m.end(1)], self._buf[m.end():]
            await self._emit(sentence)

    async def _emit(self, sentence: str):
        if self._tripped:
            return
        sentence = clean_sentence(sentence, self._allowed_tags)
        if not sentence:
            return
        if is_blocked(sentence):
            logger.warning(f"Safety filter blocked: {sentence!r}")
            self._tripped = True
            sentence = self._fallback
        self._spoke = True
        await self.push_frame(LLMTextFrame(text=sentence + " "))
