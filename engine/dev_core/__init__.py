# engine/dev_core/__init__.py
"""
Compatibility bridge for refactor.

Your canonical modules currently live in top-level `dev_core/`, but many scripts
already import them via `engine.dev_core.*`.

This file extends `engine.dev_core`'s module search path to include the legacy
top-level `dev_core/` directory, so imports like:

  from engine.dev_core.storage import connect

resolve to:

  dev_core/storage.py

No business logic changes.
"""

from __future__ import annotations

import os
from pkgutil import extend_path

# Make this a pkgutil namespace and extend search path.
__path__ = extend_path(__path__, __name__)

_here = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.abspath(os.path.join(_here, "..", ".."))
_legacy_dev_core = os.path.join(_repo_root, "dev_core")

if os.path.isdir(_legacy_dev_core) and _legacy_dev_core not in __path__:
    __path__.append(_legacy_dev_core)
