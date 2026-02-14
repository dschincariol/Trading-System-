# start_system.py
"""
Boot entrypoint (legacy)

Production rule:
- No direct subprocess launching here.
- The dashboard server owns orchestration via JobManager APIs.

This file remains as a stable entrypoint wrapper.
"""

from dotenv import load_dotenv

load_dotenv()


def main():
    from dashboard_server import run_server

    run_server()


if __name__ == "__main__":
    main()
