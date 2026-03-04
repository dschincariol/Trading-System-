"""
Enhanced Shadow Trading System
Validates models in production environment without real capital.
"""

import json
import time
import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

from engine.storage import connect, init_db
from engine.strategy.model_registry import get_stage_latest
from engine.strategy.shadow import log_shadow_prediction

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class ShadowConfig:
    max_shadow_models: int = 2
    min_shadow_samples: int = 100
    shadow_evaluation_interval_hours: int = 24
    required_shadow_days: int = 3

class ShadowTradingManager:
    """Manages shadow trading validation"""
    
    def __init__(self, config: Optional[ShadowConfig] = None):
        self.config = config or ShadowConfig()
        self._init_shadow_tables()
    
    def _init_shadow_tables(self):
        init_db()
        con = connect()
        try:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS shadow_execution_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER,
                    model_name TEXT,
                    model_kind TEXT,
                    model_ts_ms INTEGER,
                    regime TEXT,
                    symbol TEXT,
                    prediction REAL,
                    confidence REAL,
                    actual_outcome REAL,
                    pnl REAL,
                    execution_cost_bps REAL,
                    shadow_valid BOOLEAN
                );
                
                CREATE INDEX IF NOT EXISTS idx_shadow_log_model 
                    ON shadow_execution_log(model_name, ts_ms DESC);
            """)
            con.commit()
        finally:
            con.close()
    
    def run_shadow_prediction(self, model_name: str, symbol: str, features: Any):
        """Execute shadow prediction for model validation"""
        rec = get_stage_latest(model_name, "shadow", symbol=symbol)
        if not rec:
            return False
        
        try:
            pred_z, conf, meta = rec.predict(features)
            event_id = int(time.time() * 1000)
            
            log_shadow_prediction(
                event_id=event_id,
                symbol=symbol,
                horizon_s=3600,
                predicted_z=float(pred_z),
                confidence=float(conf),
                model_name=model_name,
                model_kind=getattr(rec, 'kind', None),
                model_ts_ms=getattr(rec, 'trained_ts_ms', None),
                extra={"meta": meta}
            )
            return True
        except Exception as e:
            logger.error(f"Shadow prediction failed for {model_name}: {e}")
            return False
    
    def evaluate_shadow_performance(
        self, 
        model_name: str, 
        model_kind: str, 
        model_ts_ms: int,
        regime: str = "global"
    ) -> Dict[str, Any]:
        """Evaluate shadow trading performance"""
        con = connect()
        try:
            # Get shadow performance metrics
            rows = con.execute(
                """
                SELECT prediction, actual_outcome, pnl, shadow_valid
                FROM shadow_execution_log
                WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
                ORDER BY ts_ms DESC
                LIMIT ?
                """,
                (model_name, model_kind, model_ts_ms, regime, self.config.min_shadow_samples)
            ).fetchall()
            
            if len(rows) < self.config.min_shadow_samples:
                return {"status": "insufficient_data", "samples": len(rows)}
            
            # Calculate metrics
            predictions = [float(r[0]) for r in rows if r[3]]  # shadow_valid=True
            actuals = [float(r[1]) for r in rows if r[3]]
            pnls = [float(r[2]) for r in rows if r[3]]
            
            if not predictions:
                return {"status": "no_valid_predictions"}
            
            # Performance calculations
            accuracy = sum(1 for p, a in zip(predictions, actuals) if (p > 0) == (a > 0)) / len(predictions)
            total_pnl = sum(pnls)
            sharpe = self._calculate_sharpe(pnls)
            win_rate = sum(1 for pnl in pnls if pnl > 0) / len(pnls)
            
            return {
                "status": "evaluated",
                "samples": len(predictions),
                "accuracy": accuracy,
                "total_pnl": total_pnl,
                "sharpe_ratio": sharpe,
                "win_rate": win_rate,
                "avg_pnl": total_pnl / len(pnls)
            }
            
        finally:
            con.close()
    
    def _calculate_sharpe(self, returns: List[float]) -> float:
        """Calculate Sharpe ratio from returns"""
        if not returns or len(returns) < 2:
            return 0.0
        
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / (len(returns) - 1)
        std_dev = variance ** 0.5
        
        return mean_return / std_dev if std_dev > 0 else 0.0

# Global shadow manager
_shadow_manager = None

def get_shadow_manager() -> ShadowTradingManager:
    global _shadow_manager
    if _shadow_manager is None:
        _shadow_manager = ShadowTradingManager()
    return _shadow_manager
