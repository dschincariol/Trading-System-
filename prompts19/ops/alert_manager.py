"""
Advanced Alert Manager with Thresholds and Rate Limiting

Integrates with SLOs to generate alerts with proper thresholds,
rate limiting to prevent spam, and escalation logic.
"""

import time
import json
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum
from slo_definitions import slo_evaluator, SLOStatus
from monitoring_metrics import metrics_collector

class AlertSeverity(Enum):
    INFO = "info"
    WARNING = "warning" 
    CRITICAL = "critical"
    EMERGENCY = "emergency"

@dataclass
class Alert:
    id: str
    metric_name: str
    severity: AlertSeverity
    status: SLOStatus
    current_value: float
    threshold_value: float
    message: str
    tags: Dict[str, str]
    created_ms: int
    acknowledged: bool = False
    resolved: bool = False

class AlertManager:
    """Manages alert generation, rate limiting, and lifecycle"""
    
    def __init__(self):
        self._init_tables()
        self._rate_limit_window = 300  # 5 minutes
        self._max_alerts_per_window = 10
    
    def _init_tables(self):
        """Initialize alert storage tables"""
        from engine.storage import connect
        with connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id TEXT PRIMARY KEY,
                    metric_name TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_value REAL NOT NULL,
                    threshold_value REAL NOT NULL,
                    message TEXT NOT NULL,
                    tags_json TEXT,
                    created_ms INTEGER NOT NULL,
                    acknowledged INTEGER DEFAULT 0,
                    resolved INTEGER DEFAULT 0,
                    acknowledged_ms INTEGER,
                    resolved_ms INTEGER,
                    acknowledged_by TEXT,
                    resolved_by TEXT
                )
            """)
            
            con.execute("""
                CREATE TABLE IF NOT EXISTS alert_rate_limits (
                    alert_key TEXT PRIMARY KEY,
                    last_sent_ms INTEGER NOT NULL,
                    count_sent INTEGER DEFAULT 1
                )
            """)

    def check_metric_alerts(self, metric_name: str, current_value: float,
                           tags: Optional[Dict[str, str]] = None) -> List[Alert]:
        """Check if metric triggers any alerts"""
        alerts = []
        
        # Find matching SLO
        for category, slo_dict in slo_evaluator.get_all_slos().items():
            for slo_name, slo_def in slo_dict.items():
                if slo_def.name == metric_name:
                    status = slo_evaluator.evaluate_metric(slo_def, current_value)
                    
                    if status in [SLOStatus.WARNING, SLOStatus.CRITICAL]:
                        severity = AlertSeverity.CRITICAL if status == SLOStatus.CRITICAL else AlertSeverity.WARNING
                        
                        alert = Alert(
                            id=f"{metric_name}_{int(time.time())}",
                            metric_name=metric_name,
                            severity=severity,
                            status=status,
                            current_value=current_value,
                            threshold_value=slo_def.alert_threshold_critical if status == SLOStatus.CRITICAL else slo_def.alert_threshold_warning,
                            message=f"{metric_name} {status.value}: {current_value:.2f} (threshold: {slo_def.alert_threshold_critical if status == SLOStatus.CRITICAL else slo_def.alert_threshold_warning:.2f})",
                            tags=tags or {},
                            created_ms=int(time.time() * 1000)
                        )
                        
                        if self._should_send_alert(alert):
                            alerts.append(alert)
                            self._record_rate_limit(alert)
                            self._store_alert(alert)
        
        return alerts

    def _should_send_alert(self, alert: Alert) -> bool:
        """Check if alert should be sent based on rate limiting"""
        alert_key = f"{alert.metric_name}_{alert.severity.value}"
        now_ms = int(time.time() * 1000)
        
        from engine.storage import connect
        with connect() as con:
            row = con.execute(
                "SELECT last_sent_ms, count_sent FROM alert_rate_limits WHERE alert_key = ?",
                (alert_key,)
            ).fetchone()
            
            if not row:
                return True
            
            last_sent_ms, count_sent = row
            time_since_last = now_ms - last_sent_ms
            
            # Reset window if enough time has passed
            if time_since_last > self._rate_limit_window * 1000:
                return True
            
            # Check if under rate limit
            return count_sent < self._max_alerts_per_window

    def _record_rate_limit(self, alert: Alert):
        """Record alert for rate limiting"""
        alert_key = f"{alert.metric_name}_{alert.severity.value}"
        now_ms = int(time.time() * 1000)
        
        from engine.storage import connect
        with connect() as con:
            con.execute("""
                INSERT OR REPLACE INTO alert_rate_limits (alert_key, last_sent_ms, count_sent)
                VALUES (?, ?, COALESCE(
                    (SELECT count_sent FROM alert_rate_limits WHERE alert_key = ? AND last_sent_ms > ?), 0
                ) + 1)
            """, (alert_key, now_ms, alert_key, now_ms - self._rate_limit_window * 1000))

    def _store_alert(self, alert: Alert):
        """Store alert in database"""
        from engine.storage import connect
        with connect() as con:
            con.execute("""
                INSERT OR REPLACE INTO alerts 
                (id, metric_name, severity, status, current_value, threshold_value, 
                 message, tags_json, created_ms, acknowledged, resolved)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                alert.id, alert.metric_name, alert.severity.value, alert.status.value,
                alert.current_value, alert.threshold_value, alert.message,
                json.dumps(alert.tags), alert.created_ms, 
                1 if alert.acknowledged else 0, 1 if alert.resolved else 0
            ))

    def get_active_alerts(self, severity_filter: Optional[AlertSeverity] = None) -> List[Alert]:
        """Get all active (unresolved) alerts"""
        from engine.storage import connect
        with connect() as con:
            query = "SELECT * FROM alerts WHERE resolved = 0"
            params = []
            
            if severity_filter:
                query += " AND severity = ?"
                params.append(severity_filter.value)
            
            query += " ORDER BY created_ms DESC"
            
            rows = con.execute(query, params).fetchall()
            
            alerts = []
            for row in rows:
                alerts.append(Alert(
                    id=row[0],
                    metric_name=row[1],
                    severity=AlertSeverity(row[2]),
                    status=SLOStatus(row[3]),
                    current_value=row[4],
                    threshold_value=row[5],
                    message=row[6],
                    tags=json.loads(row[7] or '{}'),
                    created_ms=row[8],
                    acknowledged=bool(row[9]),
                    resolved=bool(row[10])
                ))
            
            return alerts

# Global alert manager instance
alert_manager = AlertManager()
