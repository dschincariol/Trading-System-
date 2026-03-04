"""
Automatic Model Demotion System
Monitors live models and triggers demotion/rollback on performance degradation.
"""

import json
import time
import logging
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

from engine.storage import connect, init_db
from engine.strategy.model_registry import get_stage_latest, rollback_champion
from engine.strategy.model_governance import get_governance, DemotionTriggers
from engine.strategy.promotion_audit import audit

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DemotionAction(Enum):
    NONE = "none"
    WARNING = "warning"
    DEMOTE_TO_CHALLENGER = "demote_to_challenger"
    ROLLBACK = "rollback"
    QUARANTINE = "quarantine"

@dataclass
class DemotionConfig:
    monitoring_interval_minutes: int = 15
    performance_lookback_hours: int = 24
    consecutive_violations_for_action: int = 3
    auto_rollback_enabled: bool = True
    emergency_demotion_enabled: bool = True

class AutomaticDemotionManager:
    """Manages automatic model demotion based on performance monitoring"""
    
    def __init__(self, config: Optional[DemotionConfig] = None):
        self.config = config or DemotionConfig()
        self.governance = get_governance()
        self.demotion_triggers = DemotionTriggers()
        self._init_demotion_tables()
    
    def _init_demotion_tables(self):
        """Initialize demotion monitoring tables"""
        init_db()
        con = connect()
        try:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS model_health_monitor (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER,
                    model_name TEXT,
                    model_kind TEXT,
                    model_ts_ms INTEGER,
                    regime TEXT,
                    health_score REAL,
                    violation_type TEXT,
                    violation_value REAL,
                    threshold_value REAL,
                    severity TEXT
                );
                
                CREATE TABLE IF NOT EXISTS demotion_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER,
                    model_name TEXT,
                    model_kind TEXT,
                    model_ts_ms INTEGER,
                    regime TEXT,
                    action TEXT,
                    trigger_reason TEXT,
                    metrics_json TEXT,
                    auto_triggered BOOLEAN DEFAULT 1
                );
                
                CREATE INDEX IF NOT EXISTS idx_health_monitor_model 
                    ON model_health_monitor(model_name, ts_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_demotion_history_time 
                    ON demotion_history(ts_ms DESC);
            """)
            con.commit()
        finally:
            con.close()
    
    def monitor_all_models(self, regime: str = "global"):
        """Monitor all live champion models for performance degradation"""
        con = connect()
        try:
            # Get all champion models
            champions = con.execute(
                """
                SELECT DISTINCT model_name, model_kind, model_ts_ms
                FROM model_registry
                WHERE stage='champion' AND regime=?
                """,
                (regime,)
            ).fetchall()
            
            for model_name, model_kind, model_ts_ms in champions:
                self._monitor_model(model_name, model_kind, model_ts_ms, regime)
                
        finally:
            con.close()
    
    def _monitor_model(
        self, 
        model_name: str, 
        model_kind: str, 
        model_ts_ms: int,
        regime: str
    ) -> DemotionAction:
        """Monitor a specific model for performance issues"""
        try:
            # Get current performance metrics
            metrics = self._get_current_metrics(model_name, model_kind, model_ts_ms, regime)
            if not metrics:
                return DemotionAction.NONE
            
            # Check all demotion triggers
            violations = []
            
            # 1. Sharpe ratio degradation
            sharpe = float(metrics.get("sharpe_ratio", 0))
            if sharpe < self.demotion_triggers.sharpe_drop_threshold:
                violations.append({
                    "type": "sharpe_degradation",
                    "value": sharpe,
                    "threshold": self.demotion_triggers.sharpe_drop_threshold,
                    "severity": "HIGH" if sharpe < 0.2 else "MEDIUM"
                })
            
            # 2. Win rate degradation
            win_rate = float(metrics.get("win_rate", 0))
            if win_rate < self.demotion_triggers.win_rate_drop_threshold:
                violations.append({
                    "type": "win_rate_degradation",
                    "value": win_rate,
                    "threshold": self.demotion_triggers.win_rate_drop_threshold,
                    "severity": "HIGH" if win_rate < 0.4 else "MEDIUM"
                })
            
            # 3. Drawdown limit
            max_dd = float(metrics.get("max_drawdown", 0))
            if max_dd > self.demotion_triggers.drawdown_limit:
                violations.append({
                    "type": "drawdown_limit",
                    "value": max_dd,
                    "threshold": self.demotion_triggers.drawdown_limit,
                    "severity": "CRITICAL" if max_dd > 0.25 else "HIGH"
                })
            
            # 4. Consecutive losses
            consecutive_losses = int(metrics.get("consecutive_losses", 0))
            if consecutive_losses > self.demotion_triggers.consecutive_loss_limit:
                violations.append({
                    "type": "consecutive_losses",
                    "value": consecutive_losses,
                    "threshold": self.demotion_triggers.consecutive_loss_limit,
                    "severity": "HIGH" if consecutive_losses > 20 else "MEDIUM"
                })
            
            # 5. Daily loss limit
            daily_pnl = float(metrics.get("daily_pnl", 0))
            if daily_pnl < -self.demotion_triggers.daily_loss_limit:
                violations.append({
                    "type": "daily_loss_limit",
                    "value": daily_pnl,
                    "threshold": -self.demotion_triggers.daily_loss_limit,
                    "severity": "CRITICAL" if daily_pnl < -0.1 else "HIGH"
                })
            
            # 6. Execution quality
            exec_quality = float(metrics.get("execution_quality", 1.0))
            if exec_quality < self.demotion_triggers.min_execution_quality:
                violations.append({
                    "type": "execution_quality",
                    "value": exec_quality,
                    "threshold": self.demotion_triggers.min_execution_quality,
                    "severity": "MEDIUM"
                })
            
            # Log violations
            for violation in violations:
                self._log_health_violation(
                    model_name, model_kind, model_ts_ms, regime,
                    violation["type"], violation["value"], 
                    violation["threshold"], violation["severity"]
                )
            
            # Determine action based on violations
            if not violations:
                return DemotionAction.NONE
            
            # Check for consecutive violations
            action = self._determine_demotion_action(
                model_name, model_kind, model_ts_ms, regime, violations
            )
            
            # Execute action if needed
            if action != DemotionAction.NONE and action != DemotionAction.WARNING:
                self._execute_demotion_action(
                    model_name, model_kind, model_ts_ms, regime, 
                    action, violations, metrics
                )
            
            return action
            
        except Exception as e:
            logger.error(f"Error monitoring model {model_name}: {e}")
            return DemotionAction.NONE
    
    def _get_current_metrics(
        self, 
        model_name: str, 
        model_kind: str, 
        model_ts_ms: int,
        regime: str
    ) -> Optional[Dict[str, Any]]:
        """Get current performance metrics for a model"""
        con = connect()
        try:
            # Try to get from performance metrics table first
            row = con.execute(
                """
                SELECT sharpe_ratio, win_rate, max_drawdown, consecutive_losses,
                       daily_pnl, execution_quality
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
                    "max_drawdown": float(row[2] or 0),
                    "consecutive_losses": int(row[3] or 0),
                    "daily_pnl": float(row[4] or 0),
                    "execution_quality": float(row[5] or 1.0)
                }
            
            # Fallback to model_registry metrics
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
                    "max_drawdown": base_metrics.get("max_drawdown", 0),
                    "consecutive_losses": 0,
                    "daily_pnl": 0,
                    "execution_quality": 1.0
                }
            
            return None
            
        finally:
            con.close()
    
    def _log_health_violation(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        regime: str,
        violation_type: str,
        value: float,
        threshold: float,
        severity: str
    ):
        """Log health violation for monitoring"""
        con = connect()
        try:
            con.execute(
                """
                INSERT INTO model_health_monitor
                (ts_ms, model_name, model_kind, model_ts_ms, regime,
                 health_score, violation_type, violation_value, threshold_value, severity)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(time.time() * 1000), model_name, model_kind, model_ts_ms, regime,
                    0.0, violation_type, value, threshold, severity
                )
            )
            con.commit()
        finally:
            con.close()
    
    def _determine_demotion_action(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        regime: str,
        violations: List[Dict[str, Any]]
    ) -> DemotionAction:
        """Determine appropriate demotion action based on violations"""
        # Check for critical violations
        critical_violations = [v for v in violations if v["severity"] == "CRITICAL"]
        if critical_violations and self.config.emergency_demotion_enabled:
            return DemotionAction.ROLLBACK
        
        # Check for consecutive violations
        recent_violations = self._get_recent_violations(
            model_name, model_kind, model_ts_ms, regime
        )
        
        if len(recent_violations) >= self.config.consecutive_violations_for_action:
            # Determine action based on violation types
            high_severity_count = sum(1 for v in violations if v["severity"] == "HIGH")
            
            if high_severity_count >= 2:
                return DemotionAction.ROLLBACK
            elif high_severity_count >= 1:
                return DemotionAction.DEMOTE_TO_CHALLENGER
            else:
                return DemotionAction.WARNING
        
        return DemotionAction.WARNING
    
    def _get_recent_violations(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        regime: str
    ) -> List[Dict[str, Any]]:
        """Get recent violations for consecutive violation check"""
        con = connect()
        try:
            lookback_ms = self.config.monitoring_interval_minutes * 60 * 1000 * 2  # 2 intervals
            rows = con.execute(
                """
                SELECT violation_type, severity, ts_ms
                FROM model_health_monitor
                WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
                  AND ts_ms >= ?
                ORDER BY ts_ms DESC
                """,
                (model_name, model_kind, model_ts_ms, regime, 
                 int(time.time() * 1000) - lookback_ms)
            ).fetchall()
            
            return [
                {
                    "violation_type": row[0],
                    "severity": row[1],
                    "ts_ms": row[2]
                }
                for row in rows
            ]
            
        finally:
            con.close()
    
    def _execute_demotion_action(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        regime: str,
        action: DemotionAction,
        violations: List[Dict[str, Any]],
        metrics: Dict[str, Any]
    ):
        """Execute the determined demotion action"""
        try:
            if action == DemotionAction.ROLLBACK and self.config.auto_rollback_enabled:
                # Perform automatic rollback
                new_champion = rollback_champion(model_name, regime=regime)
                
                if new_champion:
                    # Audit the rollback
                    audit(
                        actor="auto_demotion",
                        action="rollback",
                        model_name=model_name,
                        from_kind=model_kind,
                        from_ts_ms=model_ts_ms,
                        to_kind=new_champion["model_kind"],
                        to_ts_ms=new_champion["model_ts_ms"],
                        reason={
                            "trigger": "automatic_demotion",
                            "violations": violations,
                            "metrics": metrics
                        },
                        regime=regime
                    )
                    
                    logger.warning(f"Auto-rollback executed for {model_name} due to {len(violations)} violations")
            
            elif action == DemotionAction.DEMOTE_TO_CHALLENGER:
                # Demote to challenger stage
                con = connect()
                try:
                    con.execute(
                        """
                        UPDATE model_registry
                        SET stage='challenger'
                        WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
                        """,
                        (model_name, model_kind, model_ts_ms, regime)
                    )
                    con.commit()
                finally:
                    con.close()
                
                # Audit the demotion
                audit(
                    actor="auto_demotion",
                    action="demote",
                    model_name=model_name,
                    from_kind=model_kind,
                    from_ts_ms=model_ts_ms,
                    reason={
                        "trigger": "automatic_demotion",
                        "violations": violations,
                        "metrics": metrics
                    },
                    regime=regime
                )
                
                logger.warning(f"Auto-demotion executed for {model_name} to challenger stage")
            
            # Log the action
            self._log_demotion_action(
                model_name, model_kind, model_ts_ms, regime,
                action.value, violations, metrics
            )
            
        except Exception as e:
            logger.error(f"Failed to execute demotion action {action.value} for {model_name}: {e}")
    
    def _log_demotion_action(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        regime: str,
        action: str,
        violations: List[Dict[str, Any]],
        metrics: Dict[str, Any]
    ):
        """Log demotion action for audit trail"""
        con = connect()
        try:
            con.execute(
                """
                INSERT INTO demotion_history
                (ts_ms, model_name, model_kind, model_ts_ms, regime, action,
                 trigger_reason, metrics_json, auto_triggered)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(time.time() * 1000),
                    model_name,
                    model_kind,
                    model_ts_ms,
                    regime,
                    action,
                    json.dumps(violations, separators=(",", ":"), sort_keys=True),
                    json.dumps(metrics, separators=(",", ":"), sort_keys=True),
                    True
                )
            )
            con.commit()
        finally:
            con.close()

# Global demotion manager
_demotion_manager = None

def get_demotion_manager() -> AutomaticDemotionManager:
    """Get singleton demotion manager"""
    global _demotion_manager
    if _demotion_manager is None:
        _demotion_manager = AutomaticDemotionManager()
    return _demotion_manager
