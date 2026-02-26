"""
Internal access bridge.

This module isolates dashboard from legacy imports and provides a stable
facade for internal engine access.

Refactor note:
- The new architecture does NOT use engine.
- Storage moved to engine.runtime.storage
- Execution mode moved to engine.execution.execution_mode
- Learning/relevance stats moved under engine.strategy (location varies by refactor stage)

This file must never hard-crash at import time if optional subsystems move;
it should provide stable symbols and fail with clear errors only when called.
"""

from __future__ import annotations


# ----------------------------------------------------------------------
# Storage (DB)
# ----------------------------------------------------------------------

try:
    # New architecture location
    from engine.runtime.storage import connect as db_connect  # type: ignore
    from engine.runtime.storage import init_db  # type: ignore
except Exception as _e_storage:  # pragma: no cover
    db_connect = None  # type: ignore
    init_db = None  # type: ignore
    _STORAGE_IMPORT_ERROR = _e_storage
else:
    _STORAGE_IMPORT_ERROR = None


def _require_storage():
    if db_connect is None or init_db is None:
        raise ImportError(
            "Storage backend not available. Expected engine.runtime.storage "
            "to export connect() and init_db(). Original error: "
            + repr(_STORAGE_IMPORT_ERROR)
        )


# ----------------------------------------------------------------------
# Execution mode
# ----------------------------------------------------------------------

try:
    from engine.execution.execution_mode import get_execution_mode  # type: ignore
except Exception as _e_execmode:  # pragma: no cover
    get_execution_mode = None  # type: ignore
    _EXECMODE_IMPORT_ERROR = _e_execmode
else:
    _EXECMODE_IMPORT_ERROR = None


def _require_execmode():
    if get_execution_mode is None:
        raise ImportError(
            "Execution mode not available. Expected engine.execution.execution_mode "
            "to export get_execution_mode(). Original error: "
            + repr(_EXECMODE_IMPORT_ERROR)
        )


# ----------------------------------------------------------------------
# Learning / relevance stats
# ----------------------------------------------------------------------

_learn_relevance_stats = None
_LEARN_IMPORT_ERROR = None

for _candidate in (
    # Most likely: consolidated learning utilities
    ("engine.strategy.learning", "learn_relevance_stats"),
    # Sometimes moved under relevance module
    ("engine.strategy.relevance", "learn_relevance_stats"),
    # Fallback: api module name (if it was relocated)
    ("engine.api.api_relevance", "learn_relevance_stats"),
):
    if _learn_relevance_stats is not None:
        break
    _mod_name, _fn_name = _candidate
    try:
        _m = __import__(_mod_name, fromlist=[_fn_name])
        _learn_relevance_stats = getattr(_m, _fn_name, None)
        if _learn_relevance_stats is None:
            raise AttributeError(f"{_mod_name}.{_fn_name} not found")
    except Exception as _e:  # pragma: no cover
        _LEARN_IMPORT_ERROR = _e
        _learn_relevance_stats = None


def learn_relevance_stats(*args, **kwargs):
    """
    Stable facade for relevance learning stats.

    Delegates to whichever module currently owns learn_relevance_stats().
    """
    if _learn_relevance_stats is None:
        raise ImportError(
            "learn_relevance_stats() not available. Tried:\n"
            "- engine.strategy.learning.learn_relevance_stats\n"
            "- engine.strategy.relevance.learn_relevance_stats\n"
            "- engine.api.api_relevance.learn_relevance_stats\n"
            "Original error: " + repr(_LEARN_IMPORT_ERROR)
        )
    return _learn_relevance_stats(*args, **kwargs)


# ----------------------------------------------------------------------
# Convenience wrappers (optional, but keeps old call sites stable)
# ----------------------------------------------------------------------

def connect(*args, **kwargs):
    _require_storage()
    return db_connect(*args, **kwargs)  # type: ignore[misc]


def ensure_db(*args, **kwargs):
    _require_storage()
    return init_db(*args, **kwargs)  # type: ignore[misc]


def execution_mode():
    _require_execmode()
    return get_execution_mode()  # type: ignore[misc]
