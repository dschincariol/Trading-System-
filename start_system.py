import os
import sys

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

os.environ["PYTHONPATH"] = _BASE_DIR + os.pathsep + os.environ.get("PYTHONPATH", "")

from dotenv import load_dotenv
load_dotenv()

def main():
    from dashboard_server import run_server
    run_server()

if __name__ == "__main__":
    main()