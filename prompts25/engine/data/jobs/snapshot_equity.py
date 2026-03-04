# snapshot_equity.py
from engine.equity_snapshot import snapshot_equity

if __name__ == "__main__":
    ok = snapshot_equity()
    print(f"[equity_snapshot] ok={ok}")
