"""
Advanced Rollback Management System
Fast and automatic rollback capabilities with multiple rollback strategies.
"""

import json
import time
import logging
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

from engine.storage import connect, init_db
from engine.strategy.model_registry import get_stage_latest, rollback_champion, promote_to_champion
from engine.strategy.promotion_audit import audit

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class RollbackStrategy(Enum):
    IMMEDIATE = "immediate"
    GRACEFUL = "graceful"
    STAGED = "staged"
    EMERGENCY = "emergency"

class RollbackReason(Enum):
    PERFORMANCE_DEGRADATION = "performance_degradation"
    RISK_LIMIT_BREACH = "risk_limit_breach"
    EXECUTION_FAILURE = "execution_failure"
    MANUAL_OPERATOR = "manual_operator"
    SYSTEM_ALERT = "system_alert"
    MODEL_CORRUPTION = "model_corruption"

@dataclass
class RollbackConfig:
    auto_rollback_enabled: bool = True
    rollback_timeout_seconds: int = 30
    max_rollback_attempts: int = 3
    rollback_validation_enabled: bool = True
    emergency_rollback_threshold: float = 0.1  # 10% loss triggers emergency

class RollbackManager:
    """Advanced rollback management with multiple strategies"""
    
    def __init__(self, config: Optional[RollbackConfig] = None):
        self.config = config or RollbackConfig()
        self._init_rollback_tables()
    
    def _init_rollback_tables(self):
        """Initialize rollback tracking tables"""
        init_db()
        con = connect()
        try:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS rollback_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_name TEXT,
                    model_kind TEXT,
                    model_ts_ms INTEGER,
                    regime TEXT,
                    rollback_priority INTEGER,
                    performance_score REAL,
                    stability_score REAL,
                    created_ts_ms INTEGER,
                    last_validated_ts_ms INTEGER
                );
                
                CREATE TABLE IF NOT EXISTS rollback_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER,
                    model_name TEXT,
                    regime TEXT,
                    strategy TEXT,
                    reason TEXT,
                    from_model_kind TEXT,
                    from_model_ts_ms INTEGER,
                    to_model_kind TEXT,
                    to_model_ts_ms INTEGER,
                    rollback_duration_ms INTEGER,
                    success BOOLEAN,
                    validation_metrics_json TEXT,
                    error_message TEXT
                );
                
                CREATE TABLE IF NOT EXISTS rollback_validation (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rollback_id INTEGER,
                    ts_ms INTEGER,
                    validation_type TEXT,
                    passed BOOLEAN,
                    metrics_json TEXT,
                    notes TEXT
                );
                
                CREATE INDEX IF NOT EXISTS idx_rollback_candidates_priority 
                    ON rollback_candidates(rollback_priority DESC, performance_score DESC);
                CREATE INDEX IF NOT EXISTS idx_rollback_history_time 
                    ON rollback_history(ts_ms DESC);
            """)
            con.commit()
        finally:
            con.close()
    
    def execute_rollback(
        self,
        model_name: str,
        strategy: RollbackStrategy = RollbackStrategy.IMMEDIATE,
        reason: RollbackReason = RollbackReason.PERFORMANCE_DEGRADATION,
        regime: str = "global",
        actor: str = "system"
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Execute rollback with specified strategy
        """
        start_time = int(time.time() * 1000)
        
        try:
            # Get current champion
            current_champion = get_stage_latest(model_name, "champion", regime=regime)
            if not current_champion:
                return False, {"error": "No current champion found"}
            
            # Select rollback candidate based on strategy
            candidate = self._select_rollback_candidate(
                model_name, regime, strategy, reason
            )
            
            if not candidate:
                return False, {"error": "No suitable rollback candidate found"}
            
            # Execute rollback based on strategy
            success, result = self._execute_rollback_strategy(
                model_name, regime, strategy, candidate, current_champion
            )
            
            duration = int(time.time() * 1000) - start_time
            
            # Validate rollback if enabled and successful
            validation_result = None
            if success and self.config.rollback_validation_enabled:
                validation_result = self._validate_rollback(
                    model_name, candidate, regime
                )
                if not validation_result.get("passed", False):
                    # Rollback validation failed, attempt recovery
                    logger.error(f"Rollback validation failed for {model_name}")
                    success = False
            
            # Log rollback
            self._log_rollback(
                model_name, regime, strategy, reason,
                current_champion, candidate, duration, success,
                validation_result, result.get("error")
            )
            
            # Audit the rollback
            if success:
                audit(
                    actor=actor,
                    action="rollback",
                    model_name=model_name,
                    from_kind=current_champion["model_kind"],
                    from_ts_ms=current_champion["model_ts_ms"],
                    to_kind=candidate["model_kind"],
                    to_ts_ms=candidate["model_ts_ms"],
                    reason={
                        "strategy": strategy.value,
                        "reason": reason.value,
                        "duration_ms": duration,
                        "validation": validation_result
                    },
                    regime=regime
                )
            
            return success, {
                "strategy": strategy.value,
                "candidate": candidate,
                "duration_ms": duration,
                "validation": validation_result,
                "previous_champion": current_champion
            }
            
        except Exception as e:
            logger.error(f"Rollback execution failed for {model_name}: {e}")
            return False, {"error": str(e)}
    
    def _select_rollback_candidate(
        self,
        model_name: str,
        regime: str,
        strategy: RollbackStrategy,
        reason: RollbackReason
    ) -> Optional[Dict[str, Any]]:
        """Select best rollback candidate based on strategy"""
        con = connect()
        try:
            if strategy == RollbackStrategy.IMMEDIATE:
                # Get most recent retired model
                row = con.execute(
                    """
                    SELECT model_kind, model_ts_ms, metrics_json
                    FROM model_registry
                    WHERE model_name=? AND regime=? AND stage='retired'
                    ORDER BY created_ts_ms DESC
                    LIMIT 1
                    """,
                    (model_name, regime)
                ).fetchone()
                
                if row:
                    return {
                        "model_kind": row[0],
                        "model_ts_ms": row[1],
                        "metrics": json.loads(row[2] or "{}")
                    }
            
            elif strategy == RollbackStrategy.GRACEFUL:
                # Get best performing retired model
                row = con.execute(
                    """
                    SELECT r.model_kind, r.model_ts_ms, r.metrics_json
                    FROM model_registry r
                    LEFT JOIN rollback_candidates rc ON (
                        r.model_name=rc.model_name AND 
                        r.model_kind=rc.model_kind AND 
                        r.model_ts_ms=rc.model_ts_ms
                    )
                    WHERE r.model_name=? AND r.regime=? AND r.stage='retired'
                    ORDER BY COALESCE(rc.performance_score, 0) DESC, r.created_ts_ms DESC
                    LIMIT 1
                    """,
                    (model_name, regime)
                ).fetchone()
                
                if row:
                    return {
                        "model_kind": row[0],
                        "model_ts_ms": row[1],
                        "metrics": json.loads(row[2] or "{}")
                    }
            
            elif strategy == RollbackStrategy.STAGED:
                # Get candidate with highest stability score
                row = con.execute(
                    """
                    SELECT r.model_kind, r.model_ts_ms, r.metrics_json
                    FROM model_registry r
                    LEFT JOIN rollback_candidates rc ON (
                        r.model_name=rc.model_name AND 
                        r.model_kind=rc.model_kind AND 
                        r.model_ts_ms=rc.model_ts_ms
                    )
                    WHERE r.model_name=? AND r.regime=? AND r.stage='retired'
                    ORDER BY COALESCE(rc.stability_score, 0) DESC, r.created_ts_ms DESC
                    LIMIT 1
                    """,
                    (model_name, regime)
                ).fetchone()
                
                if row:
                    return {
                        "model_kind": row[0],
                        "model_ts_ms": row[1],
                        "metrics": json.loads(row[2] or "{}")
                    }
            
            elif strategy == RollbackStrategy.EMERGENCY:
                # Get any available retired model immediately
                row = con.execute(
                    """
                    SELECT model_kind, model_ts_ms, metrics_json
                    FROM model_registry
                    WHERE model_name=? AND regime=? AND stage='retired'
                    ORDER BY created_ts_ms DESC
                    LIMIT 1
                    """,
                    (model_name, regime)
                ).fetchone()
                
                if row:
                    return {
                        "model_kind": row[0],
                        "model_ts_ms": row[1],
                        "metrics": json.loads(row[2] or "{}")
                    }
            
            return None
            
        finally:
            con.close()
    
    def _execute_rollback_strategy(
        self,
        model_name: str,
        regime: str,
        strategy: RollbackStrategy,
        candidate: Dict[str, Any],
        current_champion: Dict[str, Any]
    ) -> Tuple[bool, Dict[str, Any]]:
        """Execute rollback using the specified strategy"""
        try:
            if strategy == RollbackStrategy.IMMEDIATE:
                # Immediate rollback - no validation
                new_champion = rollback_champion(model_name, regime=regime)
                return (new_champion is not None), {}
            
            elif strategy == RollbackStrategy.GRACEFUL:
                # Graceful rollback with basic checks
                if self._validate_candidate_basic(candidate):
                    new_champion = rollback_champion(model_name, regime=regime)
                    return (new_champion is not None), {}
                else:
                    return False, {"error": "Candidate failed basic validation"}
            
            elif strategy == RollbackStrategy.STAGED:
                # Staged rollback with comprehensive validation
                validation = self._validate_candidate_comprehensive(candidate, model_name, regime)
                if validation["passed"]:
                    new_champion = rollback_champion(model_name, regime=regime)
                    return (new_champion is not None), {"validation": validation}
                else:
                    return False, {"error": "Candidate failed comprehensive validation", "validation": validation}
            
            elif strategy == RollbackStrategy.EMERGENCY:
                # Emergency rollback - force rollback
                new_champion = rollback_champion(model_name, regime=regime)
                return (new_champion is not None), {}
            
            return False, {"error": "Unknown rollback strategy"}
            
        except Exception as e:
            return False, {"error": str(e)}
    
    def _validate_candidate_basic(self, candidate: Dict[str, Any]) -> bool:
        """Basic candidate validation"""
        metrics = candidate.get("metrics", {})
        
        # Check basic performance metrics
        sharpe = float(metrics.get("sharpe", 0))
        win_rate = float(metrics.get("win_rate", 0))
        
        return sharpe > 0.1 and win_rate > 0.5
    
    def _validate_candidate_comprehensive(
        self, 
        candidate: Dict[str, Any], 
        model_name: str, 
        regime: str
    ) -> Dict[str, Any]:
        """Comprehensive candidate validation"""
        metrics = candidate.get("metrics", {})
        
        validations = {
            "sharpe_check": float(metrics.get("sharpe", 0)) > 0.2,
            "win_rate_check": float(metrics.get("win_rate", 0)) > 0.52,
            "drawdown_check": float(metrics.get("max_drawdown", 1)) < 0.15,
            "stability_check": float(metrics.get("stability_score", 0)) > 0.6,
            "sample_size_check": int(metrics.get("n_eval", 0)) > 200
        }
        
        passed = all(validations.values())
        
        return {
            "passed": passed,
            "validations": validations,
            "metrics": metrics
        }
    
    def _validate_rollback(
        self, 
        model_name: str, 
        candidate: Dict[str, Any], 
        regime: str
    ) -> Dict[str, Any]:
        """Validate rollback after execution"""
        # Check if rollback was successful
        new_champion = get_stage_latest(model_name, "champion", regime=regime)
        
        if not new_champion:
            return {"passed": False, "error": "No champion after rollback"}
        
        # Verify it's the expected model
        if (new_champion["model_kind"] != candidate["model_kind"] or 
            new_champion["model_ts_ms"] != candidate["model_ts_ms"]):
            return {"passed": False, "error": "Wrong model promoted"}
        
        # Basic health check
        return {
            "passed": True,
            "new_champion": new_champion,
            "validation_time": int(time.time() * 1000)
        }
    
    def _log_rollback(
        self,
        model_name: str,
        regime: str,
        strategy: RollbackStrategy,
        reason: RollbackReason,
        from_model: Dict[str, Any],
        to_model: Dict[str, Any],
        duration_ms: int,
        success: bool,
        validation: Optional[Dict[str, Any]],
        error_message: Optional[str]
    ):
        """Log rollback for audit trail"""
        con = connect()
        try:
            con.execute(
                """
                INSERT INTO rollback_history
                (ts_ms, model_name, regime, strategy, reason,
                 from_model_kind, from_model_ts_ms,
                 to_model_kind, to_model_ts_ms,
                 rollback_duration_ms, success, validation_metrics_json, error_message)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(time.time() * 1000),
                    model_name,
                    regime,
                    strategy.value,
                    reason.value,
                    from_model["model_kind"],
                    from_model["model_ts_ms"],
                    to_model["model_kind"],
                    to_model["model_ts_ms"],
                    duration_ms,
                    success,
                    json.dumps(validation or {}, separators=(",", ":"), sort_keys=True),
                    error_message
                )
            )
            con.commit()
        finally:
            con.close()
    
    def prepare_rollback_candidates(self, model_name: str, regime: str = "global"):
        """Pre-calculate rollback candidates for faster execution"""
        con = connect()
        try:
            # Clear existing candidates
            con.execute(
                "DELETE FROM rollback_candidates WHERE model_name=? AND regime=?",
                (model_name, regime)
            )
            
            # Get retired models
            retired_models = con.execute(
                """
                SELECT model_kind, model_ts_ms, metrics_json, created_ts_ms
                FROM model_registry
                WHERE model_name=? AND regime=? AND stage='retired'
                ORDER BY created_ts_ms DESC
                LIMIT 10
                """,
                (model_name, regime)
            ).fetchall()
            
            priority = 1
            for model_kind, model_ts_ms, metrics_json, created_ts_ms in retired_models:
                metrics = json.loads(metrics_json or "{}")
                
                # Calculate scores
                performance_score = float(metrics.get("sharpe", 0)) * float(metrics.get("win_rate", 0))
                stability_score = float(metrics.get("stability_score", 0))
                
                con.execute(
                    """
                    INSERT INTO rollback_candidates
                    (model_name, model_kind, model_ts_ms, regime, rollback_priority,
                     performance_score, stability_score, created_ts_ms, last_validated_ts_ms)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        model_name, model_kind, model_ts_ms, regime, priority,
                        performance_score, stability_score, created_ts_ms,
                        int(time.time() * 1000)
                    )
                )
                
                priority += 1
            
            con.commit()
            logger.info(f"Prepared {len(retired_models)} rollback candidates for {model_name}")
            
        finally:
            con.close()
    
    def get_rollback_history(
        self, 
        model_name: Optional[str] = None, 
        regime: str = "global",
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get rollback history"""
        con = connect()
        try:
            query = """
                SELECT ts_ms, strategy, reason, from_model_kind, from_model_ts_ms,
                       to_model_kind, to_model_ts_ms, rollback_duration_ms, success,
                       validation_metrics_json, error_message
                FROM rollback_history
                WHERE regime=?
            """
            params = [regime]
            
            if model_name:
                query += " AND model_name=?"
                params.append(model_name)
            
            query += " ORDER BY ts_ms DESC LIMIT ?"
            params.append(limit)
            
            rows = con.execute(query, params).fetchall()
            
            return [
                {
                    "ts_ms": row[0],
                    "strategy": row[1],
                    "reason": row[2],
                    "from_model_kind": row[3],
                    "from_model_ts_ms": row[4],
                    "to_model_kind": row[5],
                    "to_model_ts_ms": row[6],
                    "rollback_duration_ms": row[7],
                    "success": row[8],
                    "validation_metrics": json.loads(row[9] or "{}"),
                    "error_message": row[10]
                }
                for row in rows
            ]
            
        finally:
            con.close()

# Global rollback manager
_rollback_manager = None

def get_rollback_manager() -> RollbackManager:
    """Get singleton rollback manager"""
    global _rollback_manager
    if _rollback_manager is None:
        _rollback_manager = RollbackManager()
    return _rollback_manager
