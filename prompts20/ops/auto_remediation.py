"""
Auto-Remediation System for Live Trading

Implements automated remediation playbooks for common issues:
- Service restarts
- Component quarantine  
- Trading disable
- Data pipeline recovery
- Model rollback

Follows fail-safe principles with conservative actions.
"""

import time
import json
import subprocess
import signal
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass
from enum import Enum
from contextlib import contextmanager

class RemediationAction(Enum):
    RESTART_SERVICE = "restart_service"
    QUARANTINE_COMPONENT = "quarantine_component" 
    DISABLE_TRADING = "disable_trading"
    ENABLE_TRADING = "enable_trading"
    CLEAR_CACHE = "clear_cache"
    ROLLBACK_MODEL = "rollback_model"
    FLUSH_ORDERS = "flush_orders"
    NOTIFY_OPERATOR = "notify_operator"

class RemediationStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass
class RemediationStep:
    action: RemediationAction
    params: Dict[str, Any]
    timeout_seconds: int = 60
    retry_count: int = 0
    max_retries: int = 3

@dataclass
class RemediationPlaybook:
    name: str
    description: str
    trigger_conditions: Dict[str, Any]
    steps: List[RemediationStep]
    auto_execute: bool = True
    requires_approval: bool = False

class AutoRemediationEngine:
    """Executes automated remediation playbooks"""
    
    def __init__(self):
        self._init_tables()
        self._playbooks = self._load_default_playbooks()
        self._running_remediations = {}
    
    def _init_tables(self):
        """Initialize remediation tracking tables"""
        from engine.storage import connect
        with connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS remediation_log (
                    id TEXT PRIMARY KEY,
                    playbook_name TEXT NOT NULL,
                    trigger_alert_id TEXT,
                    status TEXT NOT NULL,
                    started_ms INTEGER NOT NULL,
                    completed_ms INTEGER,
                    steps_executed INTEGER DEFAULT 0,
                    steps_total INTEGER NOT NULL,
                    result_json TEXT,
                    error_message TEXT
                )
            """)
            
            con.execute("""
                CREATE TABLE IF NOT EXISTS component_quarantine (
                    component_name TEXT PRIMARY KEY,
                    quarantined_ms INTEGER NOT NULL,
                    reason TEXT,
                    auto_release_ms INTEGER,
                    released_ms INTEGER
                )
            """)

    def _load_default_playbooks(self) -> Dict[str, RemediationPlaybook]:
        """Load default remediation playbooks"""
        return {
            "stale_data_recovery": RemediationPlaybook(
                name="stale_data_recovery",
                description="Recover from stale market data",
                trigger_conditions={"metric_pattern": "data_freshness.*", "severity": "critical"},
                steps=[
                    RemediationStep(
                        action=RemediationAction.RESTART_SERVICE,
                        params={"service": "data_collector"},
                        timeout_seconds=30
                    ),
                    RemediationStep(
                        action=RemediationAction.CLEAR_CACHE,
                        params={"cache_type": "price_data"},
                        timeout_seconds=10
                    ),
                    RemediationStep(
                        action=RemediationAction.NOTIFY_OPERATOR,
                        params={"message": "Data collector restarted due to stale data"},
                        timeout_seconds=5
                    )
                ],
                auto_execute=True
            ),
            
            "model_drift_recovery": RemediationPlaybook(
                name="model_drift_recovery", 
                description="Recover from model drift",
                trigger_conditions={"metric_pattern": "model.model_drift", "severity": "critical"},
                steps=[
                    RemediationStep(
                        action=RemediationAction.ROLLBACK_MODEL,
                        params={"model_id": "latest", "fallback_version": "stable"},
                        timeout_seconds=45
                    ),
                    RemediationStep(
                        action=RemediationAction.DISABLE_TRADING,
                        params={"scope": "affected_symbols", "duration_minutes": 30},
                        timeout_seconds=10
                    ),
                    RemediationStep(
                        action=RemediationAction.NOTIFY_OPERATOR,
                        params={"message": "Model rolled back due to drift, trading paused"},
                        timeout_seconds=5
                    )
                ],
                auto_execute=True
            ),
            
            "execution_failure_recovery": RemediationPlaybook(
                name="execution_failure_recovery",
                description="Recover from execution failures",
                trigger_conditions={"metric_pattern": "execution.*", "severity": "critical"},
                steps=[
                    RemediationStep(
                        action=RemediationAction.FLUSH_ORDERS,
                        params={"status": "pending"},
                        timeout_seconds=15
                    ),
                    RemediationStep(
                        action=RemediationAction.QUARANTINE_COMPONENT,
                        params={"component": "execution_engine", "duration_minutes": 15},
                        timeout_seconds=10
                    ),
                    RemediationStep(
                        action=RemediationAction.RESTART_SERVICE,
                        params={"service": "execution_engine"},
                        timeout_seconds=30
                    ),
                    RemediationStep(
                        action=RemediationAction.NOTIFY_OPERATOR,
                        params={"message": "Execution engine restarted due to failures"},
                        timeout_seconds=5
                    )
                ],
                auto_execute=True
            ),
            
            "job_failure_recovery": RemediationPlaybook(
                name="job_failure_recovery",
                description="Recover from critical job failures",
                trigger_conditions={"metric_pattern": "job.*", "severity": "critical"},
                steps=[
                    RemediationStep(
                        action=RemediationAction.RESTART_SERVICE,
                        params={"service": "job_scheduler"},
                        timeout_seconds=20
                    ),
                    RemediationStep(
                        action=RemediationAction.NOTIFY_OPERATOR,
                        params={"message": "Job scheduler restarted due to failures"},
                        timeout_seconds=5
                    )
                ],
                auto_execute=True
            )
        }
    
    def trigger_remediation(self, alert_id: str, playbook_name: Optional[str] = None) -> str:
        """Trigger remediation for an alert"""
        remediation_id = f"rem_{int(time.time())}"
        
        # Find matching playbook if not specified
        if not playbook_name:
            playbook_name = self._find_matching_playbook(alert_id)
        
        if not playbook_name or playbook_name not in self._playbooks:
            raise ValueError(f"No matching playbook found for alert {alert_id}")
        
        playbook = self._playbooks[playbook_name]
        
        # Log remediation start
        from engine.storage import connect
        with connect() as con:
            con.execute("""
                INSERT INTO remediation_log 
                (id, playbook_name, trigger_alert_id, status, started_ms, steps_total)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                remediation_id, playbook_name, alert_id, 
                RemediationStatus.PENDING.value, int(time.time() * 1000),
                len(playbook.steps)
            ))
        
        # Execute if auto-enabled
        if playbook.auto_execute and not playbook.requires_approval:
            self._execute_playbook(remediation_id, playbook)
        
        return remediation_id
    
    def _find_matching_playbook(self, alert_id: str) -> Optional[str]:
        """Find playbook matching alert conditions"""
        # This would integrate with alert manager to get alert details
        # For now, return first matching based on simple pattern
        return "stale_data_recovery"  # Placeholder
    
    def _execute_playbook(self, remediation_id: str, playbook: RemediationPlaybook):
        """Execute remediation playbook steps"""
        self._running_remediations[remediation_id] = RemediationStatus.IN_PROGRESS
        
        try:
            for i, step in enumerate(playbook.steps):
                success = self._execute_step(remediation_id, step, i + 1)
                if not success:
                    self._fail_remediation(remediation_id, f"Step {i+1} failed: {step.action}")
                    return
            
            self._complete_remediation(remediation_id, RemediationStatus.SUCCESS)
            
        except Exception as e:
            self._fail_remediation(remediation_id, str(e))
        finally:
            self._running_remediations.pop(remediation_id, None)
    
    def _execute_step(self, remediation_id: str, step: RemediationStep, step_num: int) -> bool:
        """Execute individual remediation step"""
        try:
            if step.action == RemediationAction.RESTART_SERVICE:
                return self._restart_service(step.params.get("service"))
            
            elif step.action == RemediationAction.QUARANTINE_COMPONENT:
                return self._quarantine_component(step.params)
            
            elif step.action == RemediationAction.DISABLE_TRADING:
                return self._disable_trading(step.params)
            
            elif step.action == RemediationAction.ENABLE_TRADING:
                return self._enable_trading(step.params)
            
            elif step.action == RemediationAction.CLEAR_CACHE:
                return self._clear_cache(step.params)
            
            elif step.action == RemediationAction.ROLLBACK_MODEL:
                return self._rollback_model(step.params)
            
            elif step.action == RemediationAction.FLUSH_ORDERS:
                return self._flush_orders(step.params)
            
            elif step.action == RemediationAction.NOTIFY_OPERATOR:
                return self._notify_operator(step.params)
            
            return False
            
        except Exception:
            return False
    
    def _restart_service(self, service_name: str) -> bool:
        """Restart a system service"""
        try:
            # Use systemd or supervisor to restart service
            result = subprocess.run(
                ["systemctl", "restart", f"trading-{service_name}"],
                capture_output=True, text=True, timeout=30
            )
            return result.returncode == 0
        except Exception:
            return False
    
    def _quarantine_component(self, params: Dict[str, Any]) -> bool:
        """Quarantine a component"""
        component = params.get("component")
        duration_minutes = params.get("duration_minutes", 30)
        
        if not component:
            return False
        
        until_ms = int(time.time() * 1000) + duration_minutes * 60 * 1000
        
        from engine.storage import connect
        with connect() as con:
            con.execute("""
                INSERT OR REPLACE INTO component_quarantine 
                (component_name, quarantined_ms, reason, auto_release_ms)
                VALUES (?, ?, ?, ?)
            """, (
                component, int(time.time() * 1000),
                f"Auto-quarantined for {duration_minutes} minutes",
                until_ms
            ))
        
        return True
    
    def _disable_trading(self, params: Dict[str, Any]) -> bool:
        """Disable trading via kill switch"""
        from engine.execution.kill_switch import activate
        
        scope = params.get("scope", "global")
        duration_minutes = params.get("duration_minutes", 60)
        
        meta = {"until_ts_ms": int(time.time() * 1000) + duration_minutes * 60 * 1000}
        
        activate(
            scope=scope,
            key=scope if scope == "global" else "auto_disable",
            reason=f"Auto-remediation trading disable",
            actor="auto_remediation",
            meta=meta
        )
        
        return True
    
    def _enable_trading(self, params: Dict[str, Any]) -> bool:
        """Enable trading via kill switch"""
        from engine.execution.kill_switch import clear
        
        scope = params.get("scope", "global")
        
        clear(
            scope=scope,
            key=scope if scope == "global" else "auto_disable",
            reason="Auto-remediation trading enable",
            actor="auto_remediation"
        )
        
        return True
    
    def _clear_cache(self, params: Dict[str, Any]) -> bool:
        """Clear system cache"""
        cache_type = params.get("cache_type", "all")
        
        # Implementation depends on cache system used
        # This is a placeholder for Redis/memory cache clearing
        return True
    
    def _rollback_model(self, params: Dict[str, Any]) -> bool:
        """Rollback model to previous version"""
        model_id = params.get("model_id", "latest")
        fallback_version = params.get("fallback_version", "stable")
        
        # Implementation depends on model management system
        # This would trigger model rollback logic
        return True
    
    def _flush_orders(self, params: Dict[str, Any]) -> bool:
        """Flush pending orders"""
        status = params.get("status", "pending")
        
        # Implementation depends on order management system
        # This would cancel pending orders safely
        return True
    
    def _notify_operator(self, params: Dict[str, Any]) -> bool:
        """Notify human operator"""
        message = params.get("message", "Auto-remediation action taken")
        
        # Send notification via existing alert system
        from ops.alerts_service import _send_eq_crit_webhook
        
        webhook_payload = {
            "text": f"🔧 Auto-Remediation: {message}",
            "timestamp": time.time(),
            "source": "auto_remediation"
        }
        
        try:
            _send_eq_crit_webhook(webhook_payload)
            return True
        except Exception:
            return False
    
    def _complete_remediation(self, remediation_id: str, status: RemediationStatus):
        """Mark remediation as completed"""
        from engine.storage import connect
        with connect() as con:
            con.execute("""
                UPDATE remediation_log 
                SET status = ?, completed_ms = ?, steps_executed = steps_total
                WHERE id = ?
            """, (status.value, int(time.time() * 1000), remediation_id))
    
    def _fail_remediation(self, remediation_id: str, error_message: str):
        """Mark remediation as failed"""
        from engine.storage import connect
        with connect() as con:
            con.execute("""
                UPDATE remediation_log 
                SET status = ?, completed_ms = ?, error_message = ?
                WHERE id = ?
            """, (RemediationStatus.FAILED.value, int(time.time() * 1000), error_message, remediation_id))

# Global auto-remediation engine instance
auto_remediation = AutoRemediationEngine()
