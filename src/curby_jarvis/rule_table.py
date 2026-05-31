"""Deterministic utterance -> Intent lowering (the <5ms hot path; no LLM).

Compiled-regex rules cover the common verbs; a deictic token flips needs_pointer.
On a miss, `lower()` returns None and the router's IntentParseConnector escalates
to the LLM parser, which returns the SAME Intent shape.

Order matters: multi-word / specific patterns are listed before bare verbs so
"next tab" never falls into the media "next" rule.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from .intent import Intent, has_deictic

_RULES: list[tuple[re.Pattern, Callable[[re.Match], Intent]]] = []


def _rule(pattern: str, builder: Callable[[re.Match], Intent]) -> None:
    _RULES.append((re.compile(pattern, re.I), builder))


def _u(m: re.Match) -> str:
    return m.string.strip()


# -- tabs (before bare next/prev) --------------------------------------------
_rule(r"^\s*(?:switch to |go to |move to )?(?:the )?next tab\s*$",
      lambda m: Intent("switch_tab", args={"dir": "next"}, raw_utterance=_u(m)))
_rule(r"^\s*(?:switch to |go to |move to )?(?:the )?(?:previous|prev|last) tab\s*$",
      lambda m: Intent("switch_tab", args={"dir": "prev"}, raw_utterance=_u(m)))
_rule(r"^\s*(?:switch to|go to|open)\s+the\s+(?P<name>.+?)\s+tab\s*$",
      lambda m: Intent("tab_by_name", target=m.group("name").strip(),
                       args={"name": m.group("name").strip()}, raw_utterance=_u(m)))
_rule(r"^\s*new tab\s*$", lambda m: Intent("new_tab", raw_utterance=_u(m)))

# -- deictic motion (before bare move/play) ----------------------------------
_rule(r"^\s*(?:move|put)\s+(?P<a>this|that|it)\s+(?:to\s+)?(?P<b>here|there)\s*$",
      lambda m: Intent("move", needs_pointer=True, reversible=False,
                       args={"two_point": True}, raw_utterance=_u(m)))
_rule(r"^\s*drag\s+(?P<a>this|that|it)\s+(?:to\s+)?(?P<b>here|there)\s*$",
      lambda m: Intent("drag", needs_pointer=True, reversible=False,
                       args={"two_point": True}, raw_utterance=_u(m)))
_rule(r"^\s*(?:click|tap|press|select)\s+(?:on\s+)?(?P<d>this|that|here|there|it)\s*$",
      lambda m: Intent("click_at", needs_pointer=True, raw_utterance=_u(m)))

# -- app launch / search -----------------------------------------------------
_rule(r"^\s*(?:open|launch|start|fire up|bring up)\s+(?P<app>.+?)\s*$",
      lambda m: Intent("open", target=m.group("app").strip(), raw_utterance=_u(m)))
_rule(r"^\s*(?:search|google|look up|find)\s+(?:for\s+)?(?P<q>.+?)\s*$",
      lambda m: Intent("search", target=m.group("q").strip(),
                       args={"query": m.group("q").strip()}, raw_utterance=_u(m)))

# -- media transport ---------------------------------------------------------
_rule(r"^\s*(?:un)?mute\s*$", lambda m: Intent("mute", raw_utterance=_u(m)))
_rule(r"^\s*(?:turn (?:it|the volume) )?volume\s*(?P<dir>up|down)\s*$",
      lambda m: Intent("volume", args={"dir": m.group("dir").lower()}, raw_utterance=_u(m)))
_rule(r"^\s*(?:turn (?:it|the volume))\s*(?P<dir>up|down)\s*$",
      lambda m: Intent("volume", args={"dir": m.group("dir").lower()}, raw_utterance=_u(m)))
_rule(r"^\s*(?:pause|stop)(?:\s+(?:the\s+)?(?:music|song|track|it|playback))?\s*$",
      lambda m: Intent("pause", raw_utterance=_u(m)))
_rule(r"^\s*(?:next|skip)(?:\s+(?:track|song))?\s*$", lambda m: Intent("next", raw_utterance=_u(m)))
_rule(r"^\s*(?:previous|prev|back)(?:\s+(?:track|song))?\s*$", lambda m: Intent("prev", raw_utterance=_u(m)))
_rule(r"^\s*play\s+(?P<what>.+?)\s*$", lambda m: _play(m))
_rule(r"^\s*(?:play|resume)\s*$", lambda m: Intent("play", raw_utterance=_u(m)))

# -- menu commands -----------------------------------------------------------
_rule(r"^\s*close(?:\s+(?:this\s+)?(?P<thing>window|tab|it))?\s*$",
      lambda m: Intent("close", target=(m.group("thing") or "window"),
                       reversible=False, raw_utterance=_u(m)))
_rule(r"^\s*new\s+(?P<thing>doc|document|window|note|file|message|email)\s*$",
      lambda m: Intent("new", target=m.group("thing"), raw_utterance=_u(m)))
_rule(r"^\s*save(?:\s+(?:this|it))?\s*$", lambda m: Intent("save", reversible=False, raw_utterance=_u(m)))
_rule(r"^\s*select all\s*$", lambda m: Intent("select_all", raw_utterance=_u(m)))
_rule(r"^\s*(?:go )?full ?screen\s*$", lambda m: Intent("fullscreen", raw_utterance=_u(m)))
_rule(r"^\s*copy(?:\s+(?:this|it))?\s*$", lambda m: Intent("copy", raw_utterance=_u(m)))
_rule(r"^\s*paste(?:\s+(?:this|it|here))?\s*$", lambda m: Intent("paste", raw_utterance=_u(m)))
_rule(r"^\s*undo(?:\s+that)?\s*$", lambda m: Intent("undo", raw_utterance=_u(m)))


def _play(m: re.Match) -> Intent:
    what = m.group("what").strip()
    if has_deictic(what):
        # "play this" — deixis, resolved against the pointed element.
        return Intent("play", needs_pointer=True, raw_utterance=_u(m))
    return Intent("play", target=what, args={"query": what}, raw_utterance=_u(m))


def lower(utterance: str) -> Optional[Intent]:
    """Lower a raw utterance into an Intent, or None on a rule miss (-> LLM parse)."""
    if not utterance:
        return None
    for pat, builder in _RULES:
        m = pat.match(utterance)
        if m:
            return builder(m)
    return None


def fast_match(partial: str) -> bool:
    """Return True if *partial* matches a high-confidence, non-deictic rule.

    Used as the ``fast_endpoint_check`` hook in VoiceListener so that a
    clean command ('pause', 'next tab') triggers immediate finalization
    without waiting for the full silence window.

    Conditions for True:
    - ``lower(partial)`` returns a non-None Intent, AND
    - that Intent has ``confidence >= 0.9``, AND
    - ``needs_pointer`` is False (deictic commands need the pointer resolved
      before they can execute — don't rush those).
    """
    intent = lower(partial)
    if intent is None:
        return False
    if intent.needs_pointer:
        return False
    if intent.confidence < 0.9:
        return False
    return True
