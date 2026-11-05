"""BYOK provider adapters (BUILD_PLAN §1.1).

Each provider gets a subpackage (``gemini/``) behind the engine protocols in
``app.engines.base``; ``catalog.py`` is the user-facing metadata table — which
providers exist, which engines each serves, where a key comes from — that the
settings surface (task 2.1) and the registry wiring (tasks 2.2+) both build on.
"""
