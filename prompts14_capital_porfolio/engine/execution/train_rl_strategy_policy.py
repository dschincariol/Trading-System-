# train_rl_strategy_policy.py
"""
B2/C2 Milestone 1: Safe training stub.

This does NOT yet build a real dataset (Milestone 2 will).
For now:
- Initializes rl policy tables
- Writes a tiny deterministic linear model that mirrors the heuristic:
    score = -prev_drawdown
  (i.e., more drawdown -> conservative)

This enables testing storage + inference + shadow logging end-to-end.
"""

import numpy as np
from engine.rl_strategy_policy import init_rl_policy_db, upsert_policy

FEATURES = ["prev_drawdown", "avg_conf", "avg_abs_z", "n_candidates"]

def main():
    init_rl_policy_db()

    # score = (-1)*prev_drawdown + 0*others  (positive score => conservative)
    w = np.zeros((len(FEATURES),), dtype=np.float32)
    w[0] = -1.0
    bias = 0.0

    upsert_policy("v1", w, bias, n=0, feature_names=FEATURES)
    print("OK: stored rl policy v1 with features:", FEATURES)

if __name__ == "__main__":
    main()
