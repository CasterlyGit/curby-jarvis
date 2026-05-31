"""MediaTransportConnector — system-wide HID media keys for transport + volume.

cost=2 (media_key tier): cheaper than any AX/osascript route because aux HID keys
(play/pause, next, prev, mute, volume) are delivered system-wide and need NO app
focus and NO Accessibility trust — whatever app owns the Now Playing session
responds. That is why bare "next" / "mute" / "volume up" should NEVER fall through
to a menubar or AX press: the HID key is both cheaper and more robust.

Deixis carve-out: "play THIS" (intent.needs_pointer) is a point-at-an-element
action and belongs to DeixisClickConnector, so can_handle("play") returns 0.0 when
needs_pointer is set — the play media key would just toggle the global session,
not the thing the user pointed at.

"play <name>" is best-effort: we try a spotify: URI to start that track/playlist
(zero-TCC, no focus) and otherwise just send the play key so a bare resume still
works. All native bits (cgevent media keys, the URL open) are LAZY so this module
imports headless under CI.
"""
from __future__ import annotations

import time

from ..intent import (
    RISK_LAUNCH,
    RISK_REVERSIBLE,
    ConnectorResult,
    Intent,
    PreviewCard,
)
from . import Connector

# Frozen verb -> cgevent.media_key name. cgevent exposes exactly:
#   {play, next, prev, mute, sound_up, sound_down}
# HID has ONE play/pause toggle key, so 'pause' maps to the same 'play' key as
# 'play' (the OS toggles based on current transport state).
_VERB_KEY = {
    "play": "play",
    "pause": "play",
    "next": "next",
    "prev": "prev",
    "mute": "mute",
}

# volume direction (args['dir']) -> media key
_VOL_KEY = {"up": "sound_up", "down": "sound_down"}


class MediaTransportConnector(Connector):
    name = "media_key"
    cost = 2

    # -- routing --------------------------------------------------------------

    def can_handle(self, intent: Intent) -> float:
        verb = intent.verb
        if verb == "play" and intent.needs_pointer:
            # "play THIS" is deixis -> DeixisClickConnector owns it.
            return 0.0
        if verb == "volume":
            # only routable if we can resolve a direction
            return 0.95 if self._media_key_for(intent) else 0.0
        if verb in _VERB_KEY:
            return 0.95
        return 0.0

    def is_available(self, intent: Intent) -> bool:
        # Synthetic HID key posting is eaten by Secure Event Input (password
        # fields). Surface that instead of a silent no-op so the router/overlay
        # can say 'blocked' rather than pretend success.
        try:
            from ..ax.secure_input import secure_input_active

            return not secure_input_active()
        except Exception:
            # If we can't even probe, assume available — execute() still guards.
            return True

    # -- preview --------------------------------------------------------------

    def preview(self, intent: Intent) -> PreviewCard:
        key = self._media_key_for(intent)
        uri = self._spotify_uri(intent)
        if uri:
            literal = uri
            gloss = f"Spotify — {intent.target}"
            mechanism = "url"  # we'll start via the spotify: scheme
        else:
            literal = f"HID media key: {key or '?'}"
            gloss = self._gloss(intent, key)
            mechanism = self.name
        return PreviewCard(
            title=self._title(intent),
            gloss=gloss,
            mechanism=mechanism,
            risk=RISK_LAUNCH if uri else RISK_REVERSIBLE,
            literal=literal,
        )

    # -- execute --------------------------------------------------------------

    def execute(self, intent: Intent) -> ConnectorResult:
        """Send the HID media key (or best-effort spotify: URI for play <name>).

        Never raises: every OS path is wrapped so a wedged scheme handler or a
        missing cgevent symbol comes back as ok=False, not an exception that
        freezes the controller.
        """
        t0 = time.time()

        # Re-check secure input at the edge: it can flip on between preview and run.
        try:
            from ..ax.secure_input import secure_input_active

            if secure_input_active():
                return ConnectorResult(
                    ok=False, mechanism=self.name, error="secure_input_blocked",
                    latency_ms=(time.time() - t0) * 1000.0,
                )
        except Exception:
            pass

        # Best-effort: "play <name>" via spotify: URI (zero-TCC, no focus). If the
        # scheme open fails we fall through to the plain play key below.
        uri = self._spotify_uri(intent)
        if uri and self._open_uri(uri):
            return ConnectorResult(
                ok=True, mechanism="url", detail=uri,
                latency_ms=(time.time() - t0) * 1000.0,
            )

        key = self._media_key_for(intent)
        if not key:
            return ConnectorResult(
                ok=False, mechanism=self.name, error="no_media_key",
                detail=f"verb={intent.verb} dir={intent.args.get('dir')}",
                latency_ms=(time.time() - t0) * 1000.0,
            )

        try:
            from .. import cgevent  # lazy: Quartz lives behind this

            ok = bool(cgevent.media_key(key))
        except Exception as e:  # never let a native fault escape execute
            return ConnectorResult(
                ok=False, mechanism=self.name, error="media_key_error",
                detail=f"{type(e).__name__}: {e}",
                latency_ms=(time.time() - t0) * 1000.0,
            )

        return ConnectorResult(
            ok=ok,
            mechanism=self.name,
            error="" if ok else "media_key_blocked",
            detail=key,
            latency_ms=(time.time() - t0) * 1000.0,
        )

    # -- helpers --------------------------------------------------------------

    def _media_key_for(self, intent: Intent):
        """Resolve the cgevent.media_key name for this intent, or None."""
        if intent.verb == "volume":
            return _VOL_KEY.get(str(intent.args.get("dir", "")).lower())
        return _VERB_KEY.get(intent.verb)

    def _spotify_uri(self, intent: Intent):
        """A spotify: URI for 'play <name>' when a non-deictic target is present.

        Best-effort search URI: spotify:search:<terms> opens the app to the query
        so a play key (or the user) can start it. We never build this for deixis
        play (handled elsewhere) or a bare resume (no target)."""
        if intent.verb != "play" or intent.needs_pointer:
            return None
        target = (intent.target or intent.args.get("query") or "").strip()
        if not target:
            return None
        # URL-encode lazily (urllib is pure-Python/stdlib, safe at top too, but
        # keep import local to the one path that uses it).
        from urllib.parse import quote

        return "spotify:search:" + quote(target)

    def _open_uri(self, uri: str) -> bool:
        """Open a URL scheme via NSWorkspace (lazy pyobjc). False on any failure."""
        try:
            from AppKit import NSWorkspace  # lazy native import
            from Foundation import NSURL

            url = NSURL.URLWithString_(uri)
            if url is None:
                return False
            return bool(NSWorkspace.sharedWorkspace().openURL_(url))
        except Exception:
            return False

    def _title(self, intent: Intent) -> str:
        if intent.verb == "volume":
            d = str(intent.args.get("dir", "")).lower()
            return f"volume {d}".strip()
        if intent.verb == "play" and (intent.target or intent.args.get("query")):
            return f"play {intent.target or intent.args.get('query')}"
        return intent.verb

    def _gloss(self, intent: Intent, key) -> str:
        labels = {
            "play": "play / pause",
            "next": "next track",
            "prev": "previous track",
            "mute": "toggle mute",
            "sound_up": "volume up",
            "sound_down": "volume down",
        }
        return labels.get(key, intent.verb)


__all__ = ["MediaTransportConnector"]
