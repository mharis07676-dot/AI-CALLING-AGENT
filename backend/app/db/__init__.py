"""Database package.

Import engine/session from app.db.session and Base from app.db.base
to avoid eager engine creation during model imports in unit tests.
"""

__all__ = ["AsyncSessionLocal", "engine", "get_db"]


def __getattr__(name: str):
    if name in __all__:
        from app.db import session as _session

        return getattr(_session, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
