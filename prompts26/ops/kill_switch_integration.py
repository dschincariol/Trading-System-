"""
Enhanced Kill Switch Integration

Integrates monitoring and alerting with the existing kill switch system.
Provides automatic triggers based on SLO violations and critical alerts.
"""

import time
import json
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

from engine.execution.kill_switch import activate, clear, execution_allowed, snapshot
from slo_definitions import slo_evaluator, SLOStatus
from alert_manager import alert_manager, AlertSeverity
from monitoring_metrics import metrics_collector

class KillSwitchTrigger(Enum):
    SLO_VIOLATION_CRITICAL = "slo_violation_critical"
    MULTIPLE_CRITICAL_ALERTS = "multiple_critical_alerts"
    DATA_FRESHNESS_FAILURE = "data_freshness_failure"
    EXECUTION_QUALITY_FAILURE = "execution_quality_failure"
    MODEL_HEALTH_FAILURE = "model_health_failure"
    MANUAL_OPERATOR = "manual_operator"
    CAPITAL_GUARD_TRIGGER = "capital_guard_trigger"

@dataclass
class KillSwitchRule:
    name: str
    trigger_type: KillSwitchTrigger
    scope: str  # global, symbol, regime
    conditions: Dict[str, Any]
    action: str  # activate, clear
    auto_expire_minutes: Optional[int] = None
    requires_approval: bool = False

class KillSwitchIntegration:
    """Integrates monitoring with kill switch system"""
    
    def __init__(self):
        self._init_tables()
        self._rules = self._load_default_rules()
        self._alert_counts = {}
        self._last_reset_ms = int(time.time() * 1000)
    
    def _init_tables(self):
        """Initialize kill switch integration tables"""
        from engine.storage import connect
        with connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS kill_switch_triggers (
                    id TEXT PRIMARY KEY,
                    rule_name TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    triggered_by TEXT,
                    trigger_reason TEXT,
                    triggered_ms INTEGER NOT NULL,
                    expires_ms INTEGER,
                    auto_expired INTEGER DEFAULT 0
                )
            """)
            
            con.execute("""
                CREATE TABLE IF NOT EXISTS kill_switch_health (
                    id INTEGER PRIMARY KEY,
                    check_time_ms INTEGER NOT NULL,
                    trading_allowed INTEGER NOT NULL,
                    block_reason TEXT,
                    active_switches INTEGER DEFAULT 0,
                    critical_alerts INTEGER DEFAULT 0,
                    slo_violations INTEGER DEFAULT 0
                )
            """)
    
    def _load_default_rules(self) -> Dict[str, KillSwitchRule]:
        """Load default kill switch trigger rules"""
        return {
            "critical_slo_violation": KillSwitchRule(
                name="critical_slo_violation",
                trigger_type=KillSwitchTrigger.SLO_VIOLATION_CRITICAL,
                scope="global",
                conditions={
                    "min_critical_metrics": 2,
                    "consecutive_violations": 2,
                    "check_interval_minutes": 5
                },
                action="activate",
                auto_expire_minutes=30
            ),
            
            "multiple_critical_alerts": KillSwitchRule(
                name="multiple_critical_alerts", 
                trigger_type=KillSwitchTrigger.MULTIPLE_CRITICAL_ALERTS,
                scope="global",
                conditions={
                    "max_critical_alerts": 5,
                    "time_window_minutes": 10
                },
                action="activate",
                auto_expire_minutes=15
            ),
            
            "data_freshness_failure": KillSwitchRule(
                name="data_freshness_failure",
                trigger_type=KillSwitchTrigger.DATA_FRESHNESS_FAILURE,
                scope="global", 
                conditions={
                    "stale_data_types": ["price_data", "predictions"],
                    "max_age_seconds": 300
                },
                action="activate",
                auto_expire_minutes=10
            ),
            
            "execution_quality_failure": KillSwitchRule(
                name="execution_quality_failure",
                trigger_type=KillSwitchTrigger.EXECUTION_QUALITY_FAILURE,
                scope="global",
                conditions={
                    "fill_ratio_threshold": 50.0,
                    "slippage_threshold": 25.0
                },
                action="activate",
                auto_expire_minutes=20
            ),
            
            "model_health_failure": KillSwitchRule(
                name="model_health_failure",
                trigger_type=KillSwitchTrigger.MODEL_HEALTH_FAILURE,
                scope="global",
                conditions={
                    "accuracy_threshold": 50.0,
                    "drift_threshold": 0.3
                },
                action="activate",
                auto_expire_minutes=60
            )
        }
    
    def check_triggers(self) -> List[str]:
        """Check all kill switch trigger conditions"""
        triggered_rules = []
        
        for rule_name, rule in self._rules.items():
            if self._should_trigger_rule(rule):
                trigger_id = self._execute_trigger(rule)
                if trigger_id:
                    triggered_rules.append(trigger_id)
        
        return triggered_rules
    
    def _should_trigger_rule(self, rule: KillSwitchRule) -> bool:
        """Check if rule conditions are met"""
        
        if rule.trigger_type == KillSwitchTrigger.SLO_VIOLATION_CRITICAL:
            return self._check_critical_slo_violations(rule.conditions)
        
        elif rule.trigger_type == KillSwitchTrigger.MULTIPLE_CRITICAL_ALERTS:
            return self._check_multiple_critical_alerts(rule.conditions)
        
        elif rule.trigger_type == KillSwitchTrigger.DATA_FRESHNESS_FAILURE:
            return self._check_data_freshness_failure(rule.conditions)
        
        elif rule.trigger_type == KillSwitchTrigger.EXECUTION_QUALITY_FAILURE:
            return self._check_execution_quality_failure(rule.conditions)
        
        elif rule.trigger_type == KillSwitchTrigger.MODEL_HEALTH_FAILURE:
            return self._check_model_health_failure(rule.conditions)
        
        return False
    
    def _check_critical_slo_violations(self, conditions: Dict[str, Any]) -> bool:
        """Check for critical SLO violations"""
        min_critical = conditions.get("min_critical_metrics", 2)
        consecutive = conditions.get("consecutive_violations", 2)
        
        critical_count = 0
        
        # Check all SLOs
        for category, slo_dict in slo_evaluator.get_all_slos().items():
            for slo_name, slo_def in slo_dict.items():
                latest_value = metrics_collector.get_latest_value(slo_def.name)
                if latest_value is not None:
                    status = slo_evaluator.evaluate_metric(slo_def, latest_value)
                    if status == SLOStatus.CRITICAL:
                        critical_count += 1
        
        return critical_count >= min_critical
    
    def _check_multiple_critical_alerts(self, conditions: Dict[str, Any]) -> bool:
        """Check for multiple critical alerts"""
        max_alerts = conditions.get("max_critical_alerts", 5)
        time_window = conditions.get("time_window_minutes", 10)
        
        critical_alerts = alert_manager.get_active_alerts(AlertSeverity.CRITICAL)
        
        # Filter alerts within time window
        now_ms = int(time.time() * 1000)
        window_start_ms = now_ms - (time_window * 60 * 1000)
        
        recent_alerts = [
            alert for alert in critical_alerts 
            if alert.created_ms >= window_start_ms
        ]
        
        return len(recent_alerts) >= max_alerts
    
    def _check_data_freshness_failure(self, conditions: Dict[str, Any]) -> bool:
        """Check for data freshness failures"""
        stale_types = conditions.get("stale_data_types", ["price_data"])
        max_age = conditions.get("max_age_seconds", 300)
        
        for data_type in stale_types:
            latest_value = metrics_collector.get_latest_value(f"data_freshness.{data_type}")
            if latest_value is not None and latest_value > max_age:
                return True
        
        return False
    
    def _check_execution_quality_failure(self, conditions: Dict[str, Any]) -> bool:
        """Check for execution quality failures"""
        fill_threshold = conditions.get("fill_ratio_threshold", 50.0)
        slippage_threshold = conditions.get("slippage_threshold", 25.0)
        
        fill_ratio = metrics_collector.get_latest_value("execution.fill_ratio")
        slippage = metrics_collector.get_latest_value("execution.slippage")
        
        fill_failed = fill_ratio is not None and fill_ratio < fill_threshold
        slippage_failed = slippage is not None and slippage > slippage_threshold
        
        return fill_failed or slippage_failed
    
    def _check_model_health_failure(self, conditions: Dict[str, Any]) -> bool:
        """Check for model health failures"""
        accuracy_threshold = conditions.get("accuracy_threshold", 50.0)
        drift_threshold = conditions.get("drift_threshold", 0.3)
        
        accuracy = metrics_collector.get_latest_value("model.prediction_accuracy")
        drift = metrics_collector.get_latest_value("model.model_drift")
        
        accuracy_failed = accuracy is not None and accuracy < accuracy_threshold
        drift_failed = drift is not None and drift > drift_threshold
        
        return accuracy_failed or drift_failed
    
    def _execute_trigger(self, rule: KillSwitchRule) -> Optional[str]:
        """Execute kill switch trigger"""
        trigger_id = f"ks_trigger_{int(time.time())}"
        
        try:
            if rule.action == "activate":
                meta = {}
                if rule.auto_expire_minutes:
                    meta["until_ts_ms"] = int(time.time() * 1000) + rule.auto_expire_minutes * 60 * 1000
                
                activate(
                    scope=rule.scope,
                    key=rule.trigger_type.value,
                    reason=f"Auto-triggered by rule: {rule.name}",
                    actor="kill_switch_integration",
                    meta=meta
                )
                
                # Log trigger
                from engine.storage import connect
                with connect() as con:
                    con.execute("""
                        INSERT INTO kill_switch_triggers 
                        (id, rule_name, trigger_type, scope, key, action, 
                         triggered_by, trigger_reason, triggered_ms, expires_ms)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        trigger_id, rule.name, rule.trigger_type.value,
                        rule.scope, rule.trigger_type.value, rule.action,
                        "auto_system", f"Rule {rule.name} triggered",
                        int(time.time() * 1000),
                        meta.get("until_ts_ms") if rule.auto_expire_minutes else None
                    ))
                
                return trigger_id
            
        except Exception as e:
            # Log error but don't crash
            print(f"Failed to execute kill switch trigger {rule.name}: {e}")
        
        return None
    
    def check_system_health(self) -> Dict[str, Any]:
        """Check overall system health status"""
        allowed, reason, details = execution_allowed()
        
        active_alerts = alert_manager.get_active_alerts()
        critical_alerts = len([a for a in active_alerts if a.severity == AlertSeverity.CRITICAL])
        
        # Count SLO violations
        slo_violations = 0
        for category, slo_dict in slo_evaluator.get_all_slos().items():
            for slo_name, slo_def in slo_dict.items():
                latest_value = metrics_collector.get_latest_value(slo_def.name)
                if latest_value is not None:
                    status = slo_evaluator.evaluate_metric(slo_def, latest_value)
                    if status in [SLOStatus.WARNING, SLOStatus.CRITICAL]:
                        slo_violations += 1
        
        # Get active kill switches
        ks_snapshot = snapshot()
        active_switches = len([s for s in ks_snapshot.get("state", []) if s.get("enabled") == 1])
        
        health_data = {
            "trading_allowed": allowed,
            "block_reason": reason,
            "active_switches": active_switches,
            "critical_alerts": critical_alerts,
            "slo_violations": slo_violations,
            "timestamp_ms": int(time.time() * 1000)
        }
        
        # Log health check
        from engine.storage import connect
        with connect() as con:
            con.execute("""
                INSERT INTO kill_switch_health 
                (check_time_ms, trading_allowed, block_reason, active_switches, 
                 critical_alerts, slo_violations)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                health_data["timestamp_ms"], health_data["trading_allowed"],
                health_data["block_reason"], health_data["active_switches"],
                health_data["critical_alerts"], health_data["slo_violations"]
            ))
        
        return health_data
    
    def manual_trigger(self, scope: str, reason: str, 
                      duration_minutes: Optional[int] = None,
                      actor: str = "manual_operator") -> str:
        """Manual kill switch trigger by operator"""
        trigger_id = f"manual_ks_{int(time.time())}"
        
        meta = {}
        if duration_minutes:
            meta["until_ts_ms"] = int(time.time() * 1000) + duration_minutes * 60 * 1000
        
        activate(
            scope=scope,
            key="manual_trigger",
            reason=reason,
            actor=actor,
            meta=meta
        )
        
        # Log manual trigger
        from engine.storage import connect
        with connect() as con:
            con.execute("""
                INSERT INTO kill_switch_triggers 
                (id, rule_name, trigger_type, scope, key, action, 
                 triggered_by, trigger_reason, triggered_ms, expires_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trigger_id, "manual_trigger", KillSwitchTrigger.MANUAL_OPERATOR.value,
                scope, "manual_trigger", "activate",
                actor, reason, int(time.time() * 1000),
                meta.get("until_ts_ms") if duration_minutes else None
            ))
        
        return trigger_id

# Global kill switch integration instance
kill_switch_integration = KillSwitchIntegration()
