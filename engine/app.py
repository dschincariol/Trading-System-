# engine/app.py
"""
Engine entrypoint wrapper.

This repository's stable runtime entry is dashboard_server.py
(which owns HTTP + JobManager).

Keep this file so external tooling that runs `python -m engine.app`
does not break, but do not duplicate orchestration here.
"""

import os
import sys
from dotenv import load_dotenv

# Ensure repo root is importable regardless of current working directory
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

load_dotenv()


def main():
    from dashboard_server import run_server
    run_server()


if __name__ == "__main__":
    main()
