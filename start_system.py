import subprocess
import sys
import time
from dotenv import load_dotenv

load_dotenv()

JOBS = [
    "stream_prices_polygon_ws.py",
    "poll_options.py",
    "dashboard.py"
]

PROCS = []

def start_job(script):
    print(f"Starting {script}")
    p = subprocess.Popen([sys.executable, script])
    return p

def main():
    global PROCS
    try:
        for job in JOBS:
            PROCS.append(start_job(job))
            time.sleep(1)

        while True:
            time.sleep(5)

    except KeyboardInterrupt:
        print("Stopping...")
        for p in PROCS:
            try:
                p.terminate()
            except Exception:
                pass

if __name__ == "__main__":
    main()
