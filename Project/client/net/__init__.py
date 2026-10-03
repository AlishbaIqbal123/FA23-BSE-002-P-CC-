"""Client-side networking: blocking transport plus its Qt worker wrapper.

``transport``      pure-synchronous socket API, no Qt import anywhere in it
``session_worker`` the QObject moved onto a QThread, signals only
"""

__all__ = ["transport", "session_worker"]