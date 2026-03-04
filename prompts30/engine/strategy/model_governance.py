"""
Production Model Governance System
Multi-metric promotion gates, champion/challenger, shadow trading, and automatic rollback.
"""

import json
import time
import math
import os
import logging
from typing import Dict, Any, List, Tuple, Optional
from dataclasses import dataclass
from enum import Enum

from engine.storage import connect, init_db
from engine.strategy.model_registry import (
    register_model, get_stage_latest, list_recent, 
    promote_to_champion, rollback_champion
)
from engine.strategy.promotion_audit import audit
from engine.strategy.promotion_guard import promotion_allowed

# Configure logging
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [model_governance] %(message)s",
)

class ModelStage(Enum):
    CANDIDATE = "candidate"
    CHALLENGER = "challenger"
    SHADOW = "shadow"
    CHAMPION = "champion"
    RETIRED = "retired"
    QUARANTINED = "quarantined"

class PromotionDecision(Enum):
    PROMOTE = "promote"
    HOLD = "hold"
    DEMOTE = "demote"
    QUARANTINE = "quarantine"

@dataclass
class PromotionThresholds:
    """Multi-metric promotion thresholds"""
    # Profitability metrics
    min_sharpe_ratio: float = 0.5
    min_win_rate: float = 0.55
    min_profit_factor: float = 1.2
    max_acceptable_drawdown: float = 0.15  # 15%
    
    # Stability metrics
    min_sample_size: int = 500
    max_volatility: float = 0.25
    min_stability_score: float = 0.7
    
    # Performance improvement requirements
    min_sharpe_improvement: float = 0.1
    min_win_rate_improvement: float = 0.05
    max_rmse_degradation: float = 0.05
    
    # Risk metrics
    max_consecutive_losses: int = 10
    max_daily_loss_limit: float = 0.05  # 5%
    min_margin_safety: float = 1.5

@dataclass
class DemotionTriggers:
    """Automatic demotion triggers"""
    # Performance degradation
    sharpe_drop_threshold: float = 0.3
    win_rate_drop_threshold: float = 0.1
    drawdown_limit: float = 0.20  # 20%
    
    # Risk limits
    consecutive_loss_limit: int = 15
    daily_loss_limit: float = 0.08  # 8%
    
    # Stability issues
    prediction_drift_limit: float = 0.15
    volatility_spike_limit: float = 0.4
    
    # Execution quality
    max_cost_bps_limit: float = 50.0
    min_execution_quality: float = 0.6

class ModelGovernance:
    """Production model governance system"""
    
    def __init__(self):
        self.promotion_thresholds = PromotionThresholds()
        self.demotion_triggers = DemotionTriggers()
        self._init_governance_tables()
    
    def _init_governance_tables(self):
        """Initialize governance-specific database tables"""
        init_db()
        con = connect()
        try:
            # Extended model performance tracking
            con.executescript("""
                CREATE TABLE IF NOT EXISTS model_performance_metrics (
                    model_name TEXT,
                    model_kind TEXT,
                    model_ts_ms INTEGER,
                    regime TEXT DEFAULT 'global',
                    ts_ms INTEGER,
                    sharpe_ratio REAL,
                    win_rate REAL,
                    profit_factor REAL,
                    max_drawdown REAL,
                    volatility REAL,
                    stability_score REAL,
                    sample_size INTEGER,
                    consecutive_losses INTEGER,
                    daily_pnl REAL,
                    execution_cost_bps REAL,
                    execution_quality REAL,
                    PRIMARY KEY(model_name, model_kind, model_ts_ms, ts_ms)
                );
                
                CREATE TABLE IF NOT EXISTS shadow_performance (
                    model_name TEXT,
                    model_kind TEXT,
                    model_ts_ms INTEGER,
                    regime TEXT DEFAULT 'global',
                    ts_ms INTEGER,
                    shadow_pnl REAL,
                    shadow_return REAL,
                    shadow_sharpe REAL,
                    shadow_win_rate REAL,
                    vs_champion_alpha REAL,
                    prediction_accuracy REAL,
                    PRIMARY KEY(model_name, model_kind, model_ts_ms, ts_ms)
                );
                
                CREATE TABLE IF NOT EXISTS model_governance_log (
                    ts_ms INTEGER,
                    model_name TEXT,
                    model_kind TEXT,
                    model_ts_ms INTEGER,
                    regime TEXT,
                    action TEXT,
                    decision TEXT,
                    metrics_json TEXT,
                    reason TEXT,
                    actor TEXT,
                    auto_triggered BOOLEAN DEFAULT 0
                );
                
                CREATE INDEX IF NOT EXISTS idx_model_perf_name_time 
                    ON model_performance_metrics(model_name, ts_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_shadow_perf_name_time 
                    ON shadow_performance(model_name, ts_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_governance_log_time 
                    ON model_governance_log(ts_ms DESC);
            """)
            con.commit()
        finally:
            con.close()
    
    def evaluate_promotion_readiness(
        self, 
        model_name: str, 
        model_kind: str, 
        model_ts_ms: int,
        regime: str = "global"
    ) -> Tuple[PromotionDecision, Dict[str, Any]]:
        """
        Evaluate model readiness for promotion using multi-metric gates
        """
        try:
            # Get challenger metrics
            challenger_metrics = self._get_comprehensive_metrics(
                model_name, model_kind, model_ts_ms, regime
            )
            
            # Get current champion for comparison
            champion_record = get_stage_latest(model_name, "champion", regime=regime)
            champion_metrics = {}
            if champion_record:
                champion_metrics = self._get_comprehensive_metrics(
                    model_name, champion_record["model_kind"], 
                    champion_record["model_ts_ms"], regime
                )
            
            # Evaluate each gate
            gates_passed = []
            gates_failed = []
            
            # 1. Profitability Gate
            profit_decision, profit_reason = self._evaluate_profitability_gate(
                challenger_metrics, champion_metrics
            )
            if profit_decision:
                gates_passed.append("profitability")
            else:
                gates_failed.append(f"profitability: {profit_reason}")
            
            # 2. Risk Gate
            risk_decision, risk_reason = self._evaluate_risk_gate(challenger_metrics)
            if risk_decision:
                gates_passed.append("risk")
            else:
                gates_failed.append(f"risk: {risk_reason}")
            
            # 3. Stability Gate
            stability_decision, stability_reason = self._evaluate_stability_gate(challenger_metrics)
            if stability_decision:
                gates_passed.append("stability")
            else:
                gates_failed.append(f"stability: {stability_reason}")
            
            # 4. Sample Size Gate
            sample_decision, sample_reason = self._evaluate_sample_size_gate(challenger_metrics)
            if sample_decision:
                gates_passed.append("sample_size")
            else:
                gates_failed.append(f"sample_size: {sample_reason}")
            
            # 5. Improvement Gate (if champion exists)
            improvement_decision = True
            improvement_reason = "no_champion"
            if champion_metrics:
                improvement_decision, improvement_reason = self._evaluate_improvement_gate(
                    challenger_metrics, champion_metrics
                )
                if improvement_decision:
                    gates_passed.append("improvement")
                else:
                    gates_failed.append(f"improvement: {improvement_reason}")
            
            # Final decision
            all_gates_passed = len(gates_failed) == 0
            
            if all_gates_passed:
                decision = PromotionDecision.PROMOTE
                reason = f"All gates passed: {', '.join(gates_passed)}"
            elif len(gates_passed) >= 3:  # At least 75% of gates
                decision = PromotionDecision.HOLD
                reason = f"Partial success. Passed: {', '.join(gates_passed)}. Failed: {', '.join(gates_failed)}"
            else:
                decision = PromotionDecision.QUARANTINE
                reason = f"Critical failures: {', '.join(gates_failed)}"
            
            # Log evaluation
            self._log_governance_decision(
                model_name, model_kind, model_ts_ms, regime,
                "promotion_evaluation", decision.value, challenger_metrics, reason
            )
            
            return decision, {
                "decision": decision.value,
                "reason": reason,
                "gates_passed": gates_passed,
                "gates_failed": gates_failed,
                "challenger_metrics": challenger_metrics,
                "champion_metrics": champion_metrics
            }
            
        except Exception as e:
            logging.error(f"Promotion evaluation failed for {model_name}: {e}")
            return PromotionDecision.QUARANTINE, {"error": str(e)}
    
    def _evaluate_profitability_gate(
        self, 
        challenger: Dict[str, Any], 
        champion: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """Evaluate profitability metrics"""
        sharpe = float(challenger.get("sharpe_ratio", 0))
        win_rate = float(challenger.get("win_rate", 0))
        profit_factor = float(challenger.get("profit_factor", 0))
        
        if sharpe < self.promotion_thresholds.min_sharpe_ratio:
            return False, f"Sharpe ratio {sharpe:.3f} below threshold {self.promotion_thresholds.min_sharpe_ratio}"
        
        if win_rate < self.promotion_thresholds.min_win_rate:
            return False, f"Win rate {win_rate:.3f} below threshold {self.promotion_thresholds.min_win_rate}"
        
        if profit_factor < self.promotion_thresholds.min_profit_factor:
            return False, f"Profit factor {profit_factor:.3f} below threshold {self.promotion_thresholds.min_profit_factor}"
        
        return True, "Profitability metrics acceptable"
    
    def _evaluate_risk_gate(self, metrics: Dict[str, Any]) -> Tuple[bool, str]:
        """Evaluate risk metrics"""
        max_dd = float(metrics.get("max_drawdown", 1.0))
        consecutive_losses = int(metrics.get("consecutive_losses", 0))
        daily_pnl = float(metrics.get("daily_pnl", 0))
        
        if max_dd > self.promotion_thresholds.max_acceptable_drawdown:
            return False, f"Max drawdown {max_dd:.3f} exceeds limit {self.promotion_thresholds.max_acceptable_drawdown}"
        
        if consecutive_losses > self.promotion_thresholds.max_consecutive_losses:
            return False, f"Consecutive losses {consecutive_losses} exceed limit {self.promotion_thresholds.max_consecutive_losses}"
        
        if daily_pnl < -self.promotion_thresholds.max_daily_loss_limit:
            return False, f"Daily loss {daily_pnl:.3f} exceeds limit {self.promotion_thresholds.max_daily_loss_limit}"
        
        return True, "Risk metrics acceptable"
    
    def _evaluate_stability_gate(self, metrics: Dict[str, Any]) -> Tuple[bool, str]:
        """Evaluate stability metrics"""
        volatility = float(metrics.get("volatility", 1.0))
        stability_score = float(metrics.get("stability_score", 0))
        
        if volatility > self.promotion_thresholds.max_volatility:
            return False, f"Volatility {volatility:.3f} exceeds limit {self.promotion_thresholds.max_volatility}"
        
        if stability_score < self.promotion_thresholds.min_stability_score:
            return False, f"Stability score {stability_score:.3f} below threshold {self.promotion_thresholds.min_stability_score}"
        
        return True, "Stability metrics acceptable"
    
    def _evaluate_sample_size_gate(self, metrics: Dict[str, Any]) -> Tuple[bool, str]:
        """Evaluate sample size requirements"""
        sample_size = int(metrics.get("sample_size", 0))
        
        if sample_size < self.promotion_thresholds.min_sample_size:
            return False, f"Sample size {sample_size} below minimum {self.promotion_thresholds.min_sample_size}"
        
        return True, f"Sample size {sample_size} sufficient"
    
    def _evaluate_improvement_gate(
        self, 
        challenger: Dict[str, Any], 
        champion: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """Evaluate improvement over current champion"""
        ch_sharpe = float(challenger.get("sharpe_ratio", 0))
        cp_sharpe = float(champion.get("sharpe_ratio", 0))
        
        ch_win_rate = float(challenger.get("win_rate", 0))
        cp_win_rate = float(champion.get("win_rate", 0))
        
        ch_rmse = float(challenger.get("rmse", float('inf')))
        cp_rmse = float(champion.get("rmse", float('inf')))
        
        # Sharpe improvement
        if ch_sharpe < cp_sharpe + self.promotion_thresholds.min_sharpe_improvement:
            return False, f"Sharpe improvement insufficient: {ch_sharpe:.3f} vs {cp_sharpe:.3f}"
        
        # Win rate improvement
        if ch_win_rate < cp_win_rate + self.promotion_thresholds.min_win_rate_improvement:
            return False, f"Win rate improvement insufficient: {ch_win_rate:.3f} vs {cp_win_rate:.3f}"
        
        # RMSE should not degrade significantly
        if ch_rmse > cp_rmse * (1 + self.promotion_thresholds.max_rmse_degradation):
            return False, f"RMSE degradation too high: {ch_rmse:.3f} vs {cp_rmse:.3f}"
        
        return True, "Sufficient improvement over champion"
    
    def _get_comprehensive_metrics(
        self, 
        model_name: str, 
        model_kind: str, 
        model_ts_ms: int,
        regime: str
    ) -> Dict[str, Any]:
        """Get comprehensive performance metrics for a model"""
        con = connect()
        try:
            # Get latest performance metrics
            row = con.execute(
                """
                SELECT sharpe_ratio, win_rate, profit_factor, max_drawdown,
                       volatility, stability_score, sample_size, consecutive_losses,
                       daily_pnl, execution_cost_bps, execution_quality
                FROM model_performance_metrics
                WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
                ORDER BY ts_ms DESC
                LIMIT 1
                """,
                (model_name, model_kind, model_ts_ms, regime)
            ).fetchone()
            
            if row:
                return {
                    "sharpe_ratio": float(row[0] or 0),
                    "win_rate": float(row[1] or 0),
                    "profit_factor": float(row[2] or 0),
                    "max_drawdown": float(row[3] or 0),
                    "volatility": float(row[4] or 0),
                    "stability_score": float(row[5] or 0),
                    "sample_size": int(row[6] or 0),
                    "consecutive_losses": int(row[7] or 0),
                    "daily_pnl": float(row[8] or 0),
                    "execution_cost_bps": float(row[9] or 0),
                    "execution_quality": float(row[10] or 0)
                }
            
            # Fallback to basic metrics from model_registry
            model_record = con.execute(
                """
                SELECT metrics_json FROM model_registry
                WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
                """,
                (model_name, model_kind, model_ts_ms, regime)
            ).fetchone()
            
            if model_record:
                base_metrics = json.loads(model_record[0] or "{}")
                return {
                    "sharpe_ratio": base_metrics.get("sharpe", 0),
                    "win_rate": base_metrics.get("win_rate", 0),
                    "profit_factor": base_metrics.get("profit_factor", 1),
                    "max_drawdown": base_metrics.get("max_drawdown", 0),
                    "volatility": base_metrics.get("volatility", 0),
                    "stability_score": base_metrics.get("stability_score", 0),
                    "sample_size": base_metrics.get("n_eval", 0),
                    "consecutive_losses": 0,
                    "daily_pnl": 0,
                    "execution_cost_bps": 0,
                    "execution_quality": 1.0,
                    "rmse": base_metrics.get("rmse", 0)
                }
            
            return {}
            
        finally:
            con.close()
    
    def _log_governance_decision(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        regime: str,
        action: str,
        decision: str,
        metrics: Dict[str, Any],
        reason: str,
        actor: str = "system",
        auto_triggered: bool = True
    ):
        """Log governance decisions for audit trail"""
        con = connect()
        try:
            con.execute(
                """
                INSERT INTO model_governance_log
                (ts_ms, model_name, model_kind, model_ts_ms, regime, action, 
                 decision, metrics_json, reason, actor, auto_triggered)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(time.time() * 1000),
                    model_name,
                    model_kind,
                    model_ts_ms,
                    regime,
                    action,
                    decision,
                    json.dumps(metrics, separators=(",", ":"), sort_keys=True),
                    reason,
                    actor,
                    auto_triggered
                )
            )
            con.commit()
        finally:
            con.close()

# Global governance instance
_governance_instance = None

def get_governance() -> ModelGovernance:
    """Get singleton governance instance"""
    global _governance_instance
    if _governance_instance is None:
        _governance_instance = ModelGovernance()
    return _governance_instance
