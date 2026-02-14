# engine/app.py
"""
Engine entrypoint wrapper.

This repository's stable runtime entry is dashboard_server.py
(which owns HTTP + JobManager).

Keep this file so external tooling that runs `python -m engine.app`
does not break, but do not duplicate orchestration here.
"""

from dotenv import load_dotenv

load_dotenv()


def main():
    from dashboard_server import run_server

    run_server()


if __name__ == "__main__":
    main()
