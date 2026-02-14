# validate_now.py
from engine.dev_core.validation import (
    init_validation_db,
    compute_validation_scores,
    compute_model_metrics,
    get_validation_scores,
    get_model_metrics,
)

init_validation_db()
groups = compute_validation_scores()
print("validated_groups =", groups)

m_groups = compute_model_metrics(model_name="default", err_threshold=1.0, n_bins=10)
print("metrics_groups =", m_groups)

for sym, h, mae, rmse, n, ts in get_validation_scores():
    print(f"{sym} h={h} MAE={mae:.4f} RMSE={rmse:.4f} n={n}")

for r in get_model_metrics(model_name="default"):
    m = (r.get("metrics") or {})
    print(
        f'{r["symbol"]} h={r["horizon_s"]} '
        f'R2={float(m.get("r2",0.0)):.3f} '
        f'DirAcc={float(m.get("direction_acc",0.0)):.3f} '
        f'ECE={float(m.get("ece",0.0)):.3f} '
        f'AvgConf={float(m.get("avg_conf",0.0)):.3f} '
        f'n={int(r["n"])}'
    )
