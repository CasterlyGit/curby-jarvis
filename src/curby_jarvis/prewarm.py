"""TCP/TLS prewarm to the Anthropic API.

curby's api_key backend has no prewarm today (verified) — the first LLM call pays
a cold TCP+TLS handshake. We open a throwaway warm connection on startup in a
background thread so the first intent-parse round-trip is ~700ms, not ~1.5s.

Fire-and-forget; never raises, never blocks startup.
"""
from __future__ import annotations

import socket
import ssl
import threading

ANTHROPIC_HOST = "api.anthropic.com"


def prewarm(host: str = ANTHROPIC_HOST, port: int = 443, timeout: float = 3.0) -> threading.Thread:
    def _():
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=host):
                    pass
        except Exception:
            pass

    t = threading.Thread(target=_, name="anthropic-prewarm", daemon=True)
    t.start()
    return t
