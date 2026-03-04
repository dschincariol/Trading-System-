# engine/runtime/run_root_script.py
"""
Run legacy root-level scripts via engine/* wrappers (migration-safe).

Purpose:
- Preserve behavior (no rewrites)
- Stop supervisor from launching root scripts directly
- Keep root scripts present until final cleanup
"""

from __future__ import annotations

import runpy
from pathlib import Path


def repo_root() -> Path:
    # .../engine/runtime/run_root_script.py -> parents[2] == repo root
    return Path(__file__).resolve().parents[2]


def run_root_script(script_rel_path: str) -> None:
    p = (repo_root() / script_rel_path).resolve()
    runpy.run_path(str(p), run_name="__main__")
