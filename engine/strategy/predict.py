# predict.py
from engine.learning import (
    learn_relevance_stats as train_stats_from_labels,
    get_model_stats,
    confidence_from_n,
)

trained = train_stats_from_labels()
print("trained_rows =", trained)

rows = get_model_stats()
for sym, h, n, mean_z, updated_at in rows:
    conf = confidence_from_n(int(n))
    print(f"{sym} horizon_s={h} n={n} mean_impact_z={mean_z:+.3f} confidence={conf:.2f}")
