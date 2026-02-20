# start_system.py
"""
Boot entrypoint (legacy)

Production rule:
- No direct subprocess launching here.
- The dashboard server owns orchestration via JobManager APIs.

This file remains as a stable entrypoint wrapper.
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
