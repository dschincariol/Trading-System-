# FILE: start_system.py
# REPLACE ENTIRE FILE WITH THIS EXACT CONTENT:

import os
import sys

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

os.environ["PYTHONPATH"] = _BASE_DIR + os.pathsep + os.environ.get("PYTHONPATH", "")

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def _pick_mode_from_argv_or_env() -> str:
    # Prefer explicit argv, else env, else SAFE
    if len(sys.argv) >= 2 and str(sys.argv[1] or "").strip():
        return str(sys.argv[1]).strip().lower()
    v = str(os.environ.get("ENGINE_MODE", "") or "").strip().lower()
    return v or "safe"


def main():

    mode = _pick_mode_from_argv_or_env()
    os.environ["ENGINE_MODE"] = mode

    try:
        from engine.runtime.lifecycle_state import set_state, BOOTING
        set_state(BOOTING, f"mode={mode}")
    except Exception:
        pass

    # Deterministic first-run bootstrap (schema + db guard + seeding)
    try:
        from engine.runtime.first_run import bootstrap_first_run
        bootstrap_first_run(mode=mode)
    except Exception as e:
        print("FIRST_RUN_BOOTSTRAP_FAILED:", e)

    from dashboard_server import run_server
    run_server()


if __name__ == "__main__":
    main()