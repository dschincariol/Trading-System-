"""
Kill Switch Integration for Model Governance
Integrates model governance with the existing kill-switch system for emergency stops.
"""

import json
import time
import logging
from typing import Dict, Any, List, Optional

from engine.storage import connect, init_db
from engine.strategy.model_registry import get_stage_latest
from engine.strategy.rollback_manager import get_rollback_manager, RollbackStrategy, RollbackReason
from engine.strategy.automatic_demotion import get_demotion_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class KillSwitchIntegration:
    """Integrates model governance with kill-switch system"""
    
    def __init__(self):
        self.rollback_manager = get_rollback_manager()
        self.demotion_manager = get_demotion_manager()
        self._init_kill_switch_tables()
    
    def _init_kill_switch_tables(self):
        """Initialize kill-switch integration tables"""
        init_db()
        con = connect()
        try:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS governance_kill_switch_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER,
                    trigger_type TEXT,
                    model_name TEXT,
                    regime TEXT,
                    severity TEXT,
                    action_taken TEXT,
                    details_json TEXT,
                    resolved BOOLEAN DEFAULT 0
                );
                
                CREATE INDEX IF NOT EXISTS idx_kill_switch_events_time 
                    ON governance_kill_switch_events(ts_ms DESC);
            """)
            con.commit()
        finally:
            con.close()
    
    def check_kill_switch_compatibility(self, model_name: str, regime: str = "global") -> Dict[str, Any]:
        """
        Check if model governance actions respect kill-switch constraints
        """
        try:
            # Import kill-switch check
            from engine.execution.kill_switch import execution_allowed
            
            con = connect()
            
            # Get current champion
            champion = get_stage_latest(model_name, "champion", regime=regime)
            if not champion:
                return {"compatible": True, "reason": "No champion model"}
            
            # Test execution permission
            allow, reason, details = execution_allowed(con=con, regime=regime)
            
            # Check if model-specific restrictions exist
            model_restrictions = self._check_model_restrictions(model_name, regime)
            
            con.close()
            
            return {
                "compatible": allow,
                "execution_allowed": allow,
                "reason": reason,
                "details": details,
                "model_restrictions": model_restrictions,
                "champion_model": {
                    "model_kind": champion["model_kind"],
                    "model_ts_ms": champion["model_ts_ms"]
                }
            }
            
        except Exception as e:
            logger.error(f"Kill-switch compatibility check failed for {model_name}: {e}")
            return {"compatible": False, "error": str(e)}
    
    def trigger_emergency_rollback(
        self, 
        model_name: str, 
        trigger_reason: str,
        regime: str = "global"
    ) -> Dict[str, Any]:
        """
        Trigger emergency rollback due to kill-switch activation
        """
        try:
            # Log the kill-switch event
            self._log_kill_switch_event(
                trigger_type="emergency_rollback",
                model_name=model_name,
                regime=regime,
                severity="CRITICAL",
                action_taken="emergency_rollback_initiated",
                details={"trigger_reason": trigger_reason}
            )
            
            # Execute emergency rollback
            success, result = self.rollback_manager.execute_rollback(
                model_name=model_name,
                strategy=RollbackStrategy.EMERGENCY,
                reason=RollbackReason.SYSTEM_ALERT,
                regime=regime,
                actor="kill_switch"
            )
            
            # Update event log
            self._update_kill_switch_event(
                model_name, regime, 
                "emergency_rollback_completed" if success else "emergency_rollback_failed",
                {"success": success, "result": result}
            )
            
            return {
                "success": success,
                "action": "emergency_rollback",
                "result": result,
                "timestamp": int(time.time() * 1000)
            }
            
        except Exception as e:
            logger.error(f"Emergency rollback failed for {model_name}: {e}")
            return {"success": False, "error": str(e)}
    
    def trigger_emergency_demotion(
        self, 
        model_name: str, 
        trigger_reason: str,
        regime: str = "global"
    ) -> Dict[str, Any]:
        """
        Trigger emergency demotion due to kill-switch activation
        """
        try:
            # Get current champion
            champion = get_stage_latest(model_name, "champion", regime=regime)
            if not champion:
                return {"success": False, "error": "No champion found"}
            
            # Log the kill-switch event
            self._log_kill_switch_event(
                trigger_type="emergency_demotion",
                model_name=model_name,
                regime=regime,
                severity="CRITICAL",
                action_taken="emergency_demotion_initiated",
                details={"trigger_reason": trigger_reason}
            )
            
            # Force demotion to challenger stage
            con = connect()
            try:
                con.execute(
                    """
                    UPDATE model_registry
                    SET stage='quarantined'
                    WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
                    """,
                    (model_name, champion["model_kind"], champion["model_ts_ms"], regime)
                )
                con.commit()
                
                # Audit the emergency demotion
                from engine.strategy.promotion_audit import audit
                audit(
                    actor="kill_switch",
                    action="emergency_demote",
                    model_name=model_name,
                    from_kind=champion["model_kind"],
                    from_ts_ms=champion["model_ts_ms"],
                    reason={
                        "trigger": "kill_switch_emergency",
                        "trigger_reason": trigger_reason
                    },
                    regime=regime
                )
                
                success = True
                
            finally:
                con.close()
            
            # Update event log
            self._update_kill_switch_event(
                model_name, regime, 
                "emergency_demotion_completed" if success else "emergency_demotion_failed",
                {"success": success}
            )
            
            return {
                "success": success,
                "action": "emergency_demotion",
                "timestamp": int(time.time() * 1000)
            }
            
        except Exception as e:
            logger.error(f"Emergency demotion failed for {model_name}: {e}")
            return {"success": False, "error": str(e)}
    
    def validate_governance_action_with_kill_switch(
        self,
        action: str,
        model_name: str,
        model_kind: Optional[str] = None,
        model_ts_ms: Optional[int] = None,
        regime: str = "global"
    ) -> Dict[str, Any]:
        """
        Validate if a governance action is allowed under current kill-switch state
        """
        try:
            from engine.execution.kill_switch import execution_allowed
            
            con = connect()
            try:
                # Check general execution permission
                allow, reason, details = execution_allowed(con=con, regime=regime)
                
                if not allow:
                    return {
                        "allowed": False,
                        "reason": f"Kill-switch active: {reason}",
                        "kill_switch_active": True
                    }
                
                # Action-specific validations
                if action == "promote":
                    # Check if promotion is allowed during risk-off periods
                    risk_state = self._get_risk_state(regime)
                    if risk_state.get("risk_off", False):
                        return {
                            "allowed": False,
                            "reason": "Promotions blocked during risk-off period",
                            "risk_off": True
                        }
                
                elif action == "rollback":
                    # Rollbacks are generally allowed even during restrictions
                    pass
                
                elif action == "demote":
                    # Demotions are generally allowed
                    pass
                
                return {
                    "allowed": True,
                    "reason": "Action compatible with kill-switch state",
                    "kill_switch_active": False
                }
                
            finally:
                con.close()
                
        except Exception as e:
            logger.error(f"Kill-switch validation failed for {action} on {model_name}: {e}")
            return {
                "allowed": False,
                "reason": f"Validation error: {str(e)}",
                "error": str(e)
            }
    
    def _check_model_restrictions(self, model_name: str, regime: str) -> Dict[str, Any]:
        """Check for model-specific restrictions"""
        con = connect()
        try:
            # Check if model is in quarantine
            quarantine_count = con.execute(
                """
                SELECT COUNT(*) FROM model_registry
                WHERE model_name=? AND regime=? AND stage='quarantined'
                """,
                (model_name, regime)
            ).fetchone()[0]
            
            # Check recent demotions
            recent_demotions = con.execute(
                """
                SELECT COUNT(*) FROM demotion_history
                WHERE model_name=? AND regime=? AND ts_ms > ?
                """,
                (model_name, regime, int(time.time() * 1000) - 3600000)  # Last hour
            ).fetchone()[0]
            
            return {
                "in_quarantine": quarantine_count > 0,
                "recent_demotions": recent_demotions,
                "restricted": quarantine_count > 0 or recent_demotions >= 2
            }
            
        finally:
            con.close()
    
    def _get_risk_state(self, regime: str) -> Dict[str, Any]:
        """Get current risk state for the regime"""
        con = connect()
        try:
            # Check for risk-off indicators
            row = con.execute(
                """
                SELECT value FROM risk_state
                WHERE key='risk_off' AND regime=?
                """,
                (regime,)
            ).fetchone()
            
            risk_off = bool(row and row[0] == "1")
            
            # Check for critical alerts
            crit_alerts = con.execute(
                """
                SELECT COUNT(*) FROM alerts
                WHERE severity='CRIT' AND ts_ms > ?
                """,
                (int(time.time() * 1000) - 3600000,)  # Last hour
            ).fetchone()[0]
            
            return {
                "risk_off": risk_off,
                "critical_alerts": crit_alerts,
                "high_risk": risk_off or crit_alerts > 0
            }
            
        finally:
            con.close()
    
    def _log_kill_switch_event(
        self,
        trigger_type: str,
        model_name: str,
        regime: str,
        severity: str,
        action_taken: str,
        details: Dict[str, Any]
    ):
        """Log kill-switch triggered event"""
        con = connect()
        try:
            con.execute(
                """
                INSERT INTO governance_kill_switch_events
                (ts_ms, trigger_type, model_name, regime, severity, 
                 action_taken, details_json, resolved)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    int(time.time() * 1000),
                    trigger_type,
                    model_name,
                    regime,
                    severity,
                    action_taken,
                    json.dumps(details, separators=(",", ":"), sort_keys=True),
                    False
                )
            )
            con.commit()
        finally:
            con.close()
    
    def _update_kill_switch_event(
        self,
        model_name: str,
        regime: str,
        action_taken: str,
        details: Dict[str, Any]
    ):
        """Update existing kill-switch event"""
        con = connect()
        try:
            con.execute(
                """
                UPDATE governance_kill_switch_events
                SET action_taken=?, details_json=?, resolved=1
                WHERE model_name=? AND regime=? AND resolved=0
                ORDER BY ts_ms DESC
                LIMIT 1
                """,
                (
                    action_taken,
                    json.dumps(details, separators=(",", ":"), sort_keys=True),
                    model_name,
                    regime
                )
            )
            con.commit()
        finally:
            con.close()
    
    def get_kill_switch_events(
        self, 
        model_name: Optional[str] = None,
        regime: str = "global",
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get kill-switch related events"""
        con = connect()
        try:
            query = """
                SELECT ts_ms, trigger_type, model_name, regime, severity,
                       action_taken, details_json, resolved
                FROM governance_kill_switch_events
            """
            params = []
            
            if model_name:
                query += " WHERE model_name=?"
                params.append(model_name)
            
            query += " ORDER BY ts_ms DESC LIMIT ?"
            params.append(limit)
            
            rows = con.execute(query, params).fetchall()
            
            return [
                {
                    "ts_ms": row[0],
                    "trigger_type": row[1],
                    "model_name": row[2],
                    "regime": row[3],
                    "severity": row[4],
                    "action_taken": row[5],
                    "details": json.loads(row[6] or "{}"),
                    "resolved": bool(row[7])
                }
                for row in rows
            ]
            
        finally:
            con.close()

# Global kill-switch integration instance
_kill_switch_integration = None

def get_kill_switch_integration() -> KillSwitchIntegration:
    """Get singleton kill-switch integration instance"""
    global _kill_switch_integration
    if _kill_switch_integration is None:
        _kill_switch_integration = KillSwitchIntegration()
    return _kill_switch_integration
