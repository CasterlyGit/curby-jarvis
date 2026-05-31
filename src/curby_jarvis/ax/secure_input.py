"""Secure Input detector.

Password fields (login, 1Password, terminal sudo) enable Secure Event Input,
which SILENTLY swallows synthetic CGEvent keystrokes and clicks — no error, no
exception, just nothing happens. Check before any synthetic input and surface a
'blocked' state to the overlay instead of a silent no-op.

Uses ctypes against Carbon's IsSecureEventInputEnabled (no pyobjc binding needed),
so it degrades to False if the symbol can't be loaded.
"""
from __future__ import annotations

import ctypes
import ctypes.util

_fn = None
_loaded = False


def _load():
    global _fn, _loaded
    if _loaded:
        return _fn
    _loaded = True
    try:
        path = ctypes.util.find_library("Carbon") or ctypes.util.find_library("ApplicationServices")
        if not path:
            return None
        lib = ctypes.CDLL(path)
        fn = lib.IsSecureEventInputEnabled
        fn.restype = ctypes.c_bool
        fn.argtypes = []
        _fn = fn
    except Exception:
        _fn = None
    return _fn


def secure_input_active() -> bool:
    """True if Secure Event Input is engaged (synthetic keys/clicks would be eaten)."""
    fn = _load()
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:
        return False
