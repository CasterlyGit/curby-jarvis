"""NLP utility functions — pure transcript hygiene for the voice pipeline.

Before a raw STT partial or final utterance reaches the rule table or LLM
parser, it can carry filler words ('um', 'uh'), false-starts, and extra
whitespace that confuse pattern matching.  ``normalize_transcript`` strips
those noise components in-place so downstream logic sees clean imperative
text.  ``is_imperative_command`` gives a lightweight heuristic used by
``rule_table.fast_match`` and the fast-endpoint hook.

HEADLESS CONTRACT: pure Python — no imports of pyobjc, Qt, AVFoundation,
filesystem, or network at module scope or inside any function here.
"""
from __future__ import annotations

import re

# Filler words stripped from the LEADING edge only (preserving content meaning
# when they appear mid-sentence, e.g. "play, you know, that song").
_FILLERS = ("um", "uh", "er", "like", "you know")

# Compiled leading-filler pattern: one or more fillers separated by optional
# comma/space, anchored to the start.
_FILLER_PAT = re.compile(
    r"^(?:(?:" + "|".join(re.escape(f) for f in _FILLERS) + r"),?\s*)+",
    re.IGNORECASE,
)

# Simple imperative-command indicators: verbs likely to start a command.
_IMPERATIVE_STARTS = re.compile(
    r"^(?:open|close|play|pause|stop|next|prev(?:ious)?|mute|unmute|"
    r"volume|search|google|find|save|copy|paste|undo|select|fullscreen|"
    r"new|switch|go|drag|click|tap|press|launch|start|type|scroll|"
    r"resume|skip|move|put|bring)\b",
    re.IGNORECASE,
)


def normalize_transcript(text: str) -> str:
    """Return a cleaned version of *text* suitable for rule-table matching.

    Steps applied in order:
    1. Strip leading/trailing whitespace.
    2. Strip leading filler words (um, uh, er, like, you know) and any
       punctuation/comma following them.
    3. Collapse internal runs of whitespace to a single space.
    4. Drop a simple false-start: if the first word is repeated as the
       second word (e.g. "open open Spotify"), remove the duplicate.

    Content casing is preserved so downstream patterns that want to match
    proper nouns (app names, etc.) still work.
    """
    t = (text or "").strip()
    if not t:
        return t

    # Remove leading fillers.
    t = _FILLER_PAT.sub("", t).lstrip(", ").strip()
    if not t:
        return t

    # Collapse whitespace.
    t = re.sub(r"\s+", " ", t)

    # Drop repeated leading word (false-start): "open open Spotify" → "open Spotify".
    words = t.split(" ")
    if len(words) >= 2 and words[0].lower() == words[1].lower():
        t = " ".join(words[1:])

    return t


def is_imperative_command(text: str) -> bool:
    """Return True if *text* looks like a short imperative command.

    Used as a fast heuristic to decide whether a partial transcript is
    worth forwarding to the rule table immediately (fast-endpoint path).
    The check is intentionally permissive — false positives are fine
    because the rule table will return None on a real miss.
    """
    t = normalize_transcript(text)
    if not t:
        return False
    # Length guard: very long partials are probably still-evolving sentences.
    if len(t) > 120:
        return False
    return bool(_IMPERATIVE_STARTS.match(t))


__all__ = ["normalize_transcript", "is_imperative_command"]
