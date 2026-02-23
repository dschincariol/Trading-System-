# jobs/shadow_train_job.py
from engine.shadow_trainer import train_shadow

HORIZONS = [60, 300, 900]

def run():
    for h in HORIZONS:
        train_shadow(
            model_name="embed_regressor",
            horizon_s=h,
            regime=None,
        )

if __name__ == "__main__":
    run()
