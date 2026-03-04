"""
Champion/Challenger Framework
Manages model competition, automatic promotion, and performance comparison.
"""

import json
import time
import logging
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

from engine.storage import connect, init_db
from engine.strategy.model_registry import (
    register_model, get_stage_latest, list_recent, 
    promote_to_champion, rollback_champion
)
from engine.strategy.model_governance import get_governance, PromotionDecision
from engine.strategy.promotion_audit import audit
from engine.strategy.promotion_guard import promotion_allowed

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class ChallengerConfig:
    """Configuration for challenger management"""
    max_challengers_per_model: int = 3
    min_challenger_age_hours: int = 24
    max_challenger_age_hours: int = 168  # 1 week
    promotion_evaluation_interval_hours: int = 6
    auto_promote_enabled: bool = True
    require_shadow_validation: bool = True

class CompetitionStatus(Enum):
    PENDING = "pending"
    ACTIVE = "active"
    EVALUATING = "evaluating"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    EXPIRED = "expired"

class ChampionChallengerManager:
    """Manages champion/challenger model competition"""
    
    def __init__(self, config: Optional[ChallengerConfig] = None):
        self.config = config or ChallengerConfig()
        self.governance = get_governance()
        self._init_competition_tables()
    
    def _init_competition_tables(self):
        """Initialize competition tracking tables"""
        init_db()
        con = connect()
        try:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS model_competition (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_name TEXT,
                    model_kind TEXT,
                    model_ts_ms INTEGER,
                    regime TEXT DEFAULT 'global',
                    status TEXT,
                    created_ts_ms INTEGER,
                    last_evaluated_ts_ms INTEGER,
                    promotion_score REAL,
                    competition_metrics_json TEXT,
                    notes TEXT,
                    UNIQUE(model_name, model_kind, model_ts_ms, regime)
                );
                
                CREATE TABLE IF NOT EXISTS model_comparison (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_name TEXT,
                    regime TEXT,
                    ts_ms INTEGER,
                    champion_kind TEXT,
                    champion_ts_ms INTEGER,
                    challenger_kind TEXT,
                    challenger_ts_ms INTEGER,
                    comparison_metrics_json TEXT,
                    winner TEXT,
                    margin REAL
                );
                
                CREATE INDEX IF NOT EXISTS idx_competition_status 
                    ON model_competition(status, created_ts_ms);
                CREATE INDEX IF NOT EXISTS idx_competition_model 
                    ON model_competition(model_name, regime, status);
                CREATE INDEX IF NOT EXISTS idx_comparison_time 
                    ON model_comparison(ts_ms DESC);
            """)
            con.commit()
        finally:
            con.close()
    
    def register_challenger(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        metrics: Dict[str, Any],
        regime: str = "global",
        note: Optional[str] = None
    ) -> bool:
        """
        Register a new challenger model for competition
        """
        try:
            # Check if we already have too many challengers
            existing_challengers = self._get_active_challengers(model_name, regime)
            if len(existing_challengers) >= self.config.max_challengers_per_model:
                # Remove oldest challenger
                oldest = min(existing_challengers, key=lambda x: x['created_ts_ms'])
                self._retire_challenger(
                    model_name, oldest['model_kind'], 
                    oldest['model_ts_ms'], regime, "max_challengers_exceeded"
                )
            
            # Register in model_registry as challenger
            register_model(
                model_name=model_name,
                model_kind=model_kind,
                model_ts_ms=model_ts_ms,
                stage="challenger",
                metrics=metrics,
                note=note,
                regime=regime
            )
            
            # Register in competition table
            con = connect()
            try:
                con.execute(
                    """
                    INSERT OR REPLACE INTO model_competition
                    (model_name, model_kind, model_ts_ms, regime, status, 
                     created_ts_ms, last_evaluated_ts_ms, competition_metrics_json, notes)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        model_name, model_kind, model_ts_ms, regime,
                        CompetitionStatus.PENDING.value,
                        int(time.time() * 1000),
                        int(time.time() * 1000),
                        json.dumps(metrics, separators=(",", ":"), sort_keys=True),
                        note
                    )
                )
                con.commit()
            finally:
                con.close()
            
            logger.info(f"Registered challenger: {model_name}/{model_kind} for regime {regime}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to register challenger {model_name}/{model_kind}: {e}")
            return False
    
    def evaluate_challenger_promotion(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        regime: str = "global"
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Evaluate if a challenger should be promoted to champion
        """
        try:
            # Check system-wide promotion guard
            promo_allowed, promo_reason = promotion_allowed()
            if not promo_allowed:
                return False, {"reason": "system_guard", "details": promo_reason}
            
            # Get governance evaluation
            decision, eval_result = self.governance.evaluate_promotion_readiness(
                model_name, model_kind, model_ts_ms, regime
            )
            
            if decision == PromotionDecision.PROMOTE:
                # Additional checks
                if self.config.require_shadow_validation:
                    shadow_valid = self._validate_shadow_performance(
                        model_name, model_kind, model_ts_ms, regime
                    )
                    if not shadow_valid:
                        decision = PromotionDecision.HOLD
                        eval_result["reason"] += " | Shadow validation failed"
            
            # Update competition status
            self._update_competition_status(
                model_name, model_kind, model_ts_ms, regime, decision
            )
            
            # Perform promotion if decided
            if decision == PromotionDecision.PROMOTE and self.config.auto_promote_enabled:
                success = self._promote_to_champion(
                    model_name, model_kind, model_ts_ms, regime, eval_result
                )
                return success, eval_result
            
            return decision == PromotionDecision.PROMOTE, eval_result
            
        except Exception as e:
            logger.error(f"Failed to evaluate promotion for {model_name}/{model_kind}: {e}")
            return False, {"error": str(e)}
    
    def _promote_to_champion(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        regime: str,
        eval_result: Dict[str, Any]
    ) -> bool:
        """Promote challenger to champion"""
        try:
            # Get current champion for comparison
            current_champion = get_stage_latest(model_name, "champion", regime=regime)
            
            # Perform promotion
            promote_to_champion(model_name, model_kind, model_ts_ms, regime=regime)
            
            # Audit the promotion
            audit(
                actor="auto_promotion",
                action="promote",
                model_name=model_name,
                from_kind=(current_champion["model_kind"] if current_champion else None),
                from_ts_ms=(current_champion["model_ts_ms"] if current_champion else None),
                to_kind=model_kind,
                to_ts_ms=model_ts_ms,
                reason=eval_result,
                regime=regime
            )
            
            # Log comparison
            if current_champion:
                self._log_model_comparison(
                    model_name, regime, current_champion, 
                    {"model_kind": model_kind, "model_ts_ms": model_ts_ms},
                    eval_result
                )
            
            logger.info(f"Promoted {model_name}/{model_kind} to champion for regime {regime}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to promote {model_name}/{model_kind}: {e}")
            return False
    
    def _validate_shadow_performance(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        regime: str
    ) -> bool:
        """Validate challenger performance in shadow trading"""
        con = connect()
        try:
            # Get shadow performance metrics
            row = con.execute(
                """
                SELECT shadow_sharpe, shadow_win_rate, vs_champion_alpha,
                       prediction_accuracy
                FROM shadow_performance
                WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
                ORDER BY ts_ms DESC
                LIMIT 1
                """,
                (model_name, model_kind, model_ts_ms, regime)
            ).fetchone()
            
            if not row:
                return False  # No shadow data available
            
            shadow_sharpe = float(row[0] or 0)
            shadow_win_rate = float(row[1] or 0)
            alpha = float(row[2] or 0)
            accuracy = float(row[3] or 0)
            
            # Minimum shadow performance thresholds
            min_shadow_sharpe = 0.3
            min_shadow_win_rate = 0.52
            min_alpha = 0.02
            min_accuracy = 0.55
            
            return (shadow_sharpe >= min_shadow_sharpe and
                   shadow_win_rate >= min_shadow_win_rate and
                   alpha >= min_alpha and
                   accuracy >= min_accuracy)
            
        finally:
            con.close()
    
    def _get_active_challengers(self, model_name: str, regime: str) -> List[Dict[str, Any]]:
        """Get list of active challengers for a model"""
        con = connect()
        try:
            rows = con.execute(
                """
                SELECT model_kind, model_ts_ms, created_ts_ms, status
                FROM model_competition
                WHERE model_name=? AND regime=? AND status IN ('pending', 'active', 'evaluating')
                ORDER BY created_ts_ms DESC
                """,
                (model_name, regime)
            ).fetchall()
            
            return [
                {
                    "model_kind": row[0],
                    "model_ts_ms": row[1],
                    "created_ts_ms": row[2],
                    "status": row[3]
                }
                for row in rows
            ]
            
        finally:
            con.close()
    
    def _retire_challenger(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        regime: str,
        reason: str
    ):
        """Retire a challenger"""
        con = connect()
        try:
            # Update competition status
            con.execute(
                """
                UPDATE model_competition
                SET status='expired', notes=COALESCE(notes, '') || ? || '; '
                WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
                """,
                (f"Retired: {reason}", model_name, model_kind, model_ts_ms, regime)
            )
            
            # Update model_registry stage
            con.execute(
                """
                UPDATE model_registry
                SET stage='retired'
                WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
                """,
                (model_name, model_kind, model_ts_ms, regime)
            )
            
            con.commit()
            
        finally:
            con.close()
    
    def _update_competition_status(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        regime: str,
        decision: PromotionDecision
    ):
        """Update competition status based on evaluation"""
        status_map = {
            PromotionDecision.PROMOTE: CompetitionStatus.PROMOTED,
            PromotionDecision.HOLD: CompetitionStatus.EVALUATING,
            PromotionDecision.DEMOTE: CompetitionStatus.REJECTED,
            PromotionDecision.QUARANTINE: CompetitionStatus.REJECTED
        }
        
        status = status_map.get(decision, CompetitionStatus.EVALUATING)
        
        con = connect()
        try:
            con.execute(
                """
                UPDATE model_competition
                SET status=?, last_evaluated_ts_ms=?
                WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
                """,
                (status.value, int(time.time() * 1000), model_name, model_kind, model_ts_ms, regime)
            )
            con.commit()
        finally:
            con.close()
    
    def _log_model_comparison(
        self,
        model_name: str,
        regime: str,
        champion: Dict[str, Any],
        challenger: Dict[str, Any],
        eval_result: Dict[str, Any]
    ):
        """Log comparison between champion and challenger"""
        con = connect()
        try:
            # Calculate performance margin
            challenger_metrics = eval_result.get("challenger_metrics", {})
            champion_metrics = eval_result.get("champion_metrics", {})
            
            sharpe_margin = float(challenger_metrics.get("sharpe_ratio", 0)) - float(champion_metrics.get("sharpe_ratio", 0))
            
            con.execute(
                """
                INSERT INTO model_comparison
                (model_name, regime, ts_ms, champion_kind, champion_ts_ms,
                 challenger_kind, challenger_ts_ms, comparison_metrics_json, winner, margin)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    model_name, regime, int(time.time() * 1000),
                    champion["model_kind"], champion["model_ts_ms"],
                    challenger["model_kind"], challenger["model_ts_ms"],
                    json.dumps(eval_result, separators=(",", ":"), sort_keys=True),
                    "challenger", sharpe_margin
                )
            )
            con.commit()
        finally:
            con.close()
    
    def run_evaluation_cycle(self, model_name: Optional[str] = None, regime: str = "global"):
        """Run evaluation cycle for all or specific model challengers"""
        con = connect()
        try:
            if model_name:
                # Evaluate specific model
                challengers = self._get_active_challengers(model_name, regime)
                self._evaluate_challengers(model_name, regime, challengers)
            else:
                # Evaluate all models with active challengers
                models = con.execute(
                    """
                    SELECT DISTINCT model_name, regime
                    FROM model_competition
                    WHERE status IN ('pending', 'active', 'evaluating')
                    """
                ).fetchall()
                
                for model_name, reg in models:
                    challengers = self._get_active_challengers(model_name, reg)
                    self._evaluate_challengers(model_name, reg, challengers)
                    
        finally:
            con.close()
    
    def _evaluate_challengers(
        self,
        model_name: str,
        regime: str,
        challengers: List[Dict[str, Any]]
    ):
        """Evaluate a list of challengers for a model"""
        for challenger in challengers:
            # Check if challenger is old enough for evaluation
            age_hours = (time.time() * 1000 - challenger["created_ts_ms"]) / (1000 * 60 * 60)
            if age_hours < self.config.min_challenger_age_hours:
                continue
            
            # Check if challenger is too old
            if age_hours > self.config.max_challenger_age_hours:
                self._retire_challenger(
                    model_name, challenger["model_kind"],
                    challenger["model_ts_ms"], regime, "expired"
                )
                continue
            
            # Evaluate for promotion
            self.evaluate_challenger_promotion(
                model_name, challenger["model_kind"],
                challenger["model_ts_ms"], regime
            )

# Global manager instance
_manager_instance = None

def get_champion_challenger_manager() -> ChampionChallengerManager:
    """Get singleton champion/challenger manager"""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = ChampionChallengerManager()
    return _manager_instance
