"""
Monitoring Orchestrator - Main Monitoring System Coordinator

Coordinates all monitoring components:
- SLO evaluation and metrics collection
- Alert generation and management
- Auto-remediation execution
- Kill switch integration
- Escalation processing
- Dashboard data serving

Runs continuously to monitor system health and trigger appropriate responses.
"""

import time
import threading
import json
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

from slo_definitions import slo_evaluator, SLOStatus
from monitoring_metrics import metrics_collector
from alert_manager import alert_manager, AlertSeverity
from auto_remediation import auto_remediation
from kill_switch_integration import kill_switch_integration
from escalation_rules import escalation_manager

class MonitoringOrchestrator:
    """Main monitoring system coordinator"""
    
    def __init__(self):
        self.running = False
        self.monitor_thread = None
        self.check_interval_seconds = 30  # Check every 30 seconds
        self.last_check_ms = 0
        
        # Component status tracking
        self.component_status = {
            "metrics_collector": True,
            "alert_manager": True,
            "auto_remediation": True,
            "kill_switch_integration": True,
            "escalation_manager": True
        }
    
    def start(self):
        """Start the monitoring orchestrator"""
        if self.running:
            return
        
        self.running = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        print("Monitoring orchestrator started")
    
    def stop(self):
        """Stop the monitoring orchestrator"""
        self.running = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=10)
        print("Monitoring orchestrator stopped")
    
    def _monitor_loop(self):
        """Main monitoring loop"""
        while self.running:
            try:
                start_time = time.time()
                
                # Run monitoring cycle
                self._run_monitoring_cycle()
                
                # Calculate sleep time to maintain interval
                cycle_time = time.time() - start_time
                sleep_time = max(0, self.check_interval_seconds - cycle_time)
                
                if sleep_time > 0:
                    time.sleep(sleep_time)
                
            except Exception as e:
                print(f"Error in monitoring loop: {e}")
                time.sleep(self.check_interval_seconds)
    
    def _run_monitoring_cycle(self):
        """Run one complete monitoring cycle"""
        cycle_start_ms = int(time.time() * 1000)
        
        # 1. Collect system metrics
        self._collect_system_metrics()
        
        # 2. Evaluate SLOs and check for violations
        slo_violations = self._evaluate_slos()
        
        # 3. Check for alerts and trigger notifications
        new_alerts = self._check_alerts()
        
        # 4. Process auto-remediation
        self._process_auto_remediation(new_alerts)
        
        # 5. Check kill switch triggers
        ks_triggers = self._check_kill_switch_triggers()
        
        # 6. Process escalations
        self._process_escalations(new_alerts)
        
        # 7. Record cycle completion
        self._record_monitoring_cycle(cycle_start_ms, slo_violations, new_alerts, ks_triggers)
    
    def _collect_system_metrics(self):
        """Collect system-wide metrics"""
        try:
            # Data freshness metrics
            self._collect_data_freshness_metrics()
            
            # Model health metrics
            self._collect_model_health_metrics()
            
            # Execution quality metrics
            self._collect_execution_metrics()
            
            # Job reliability metrics
            self._collect_job_metrics()
            
            # System health metrics
            self._collect_system_health_metrics()
            
        except Exception as e:
            print(f"Error collecting metrics: {e}")
    
    def _collect_data_freshness_metrics(self):
        """Collect data freshness metrics"""
        # Price data freshness
        latest_price_age = self._get_latest_data_age("prices")
        if latest_price_age is not None:
            metrics_collector.record_data_freshness("price_data", latest_price_age)
        
        # Events freshness
        latest_event_age = self._get_latest_data_age("events")
        if latest_event_age is not None:
            metrics_collector.record_data_freshness("market_data", latest_event_age)
        
        # Predictions freshness
        latest_pred_age = self._get_latest_data_age("predictions")
        if latest_pred_age is not None:
            metrics_collector.record_data_freshness("predictions", latest_pred_age)
    
    def _collect_model_health_metrics(self):
        """Collect model health metrics"""
        # Model accuracy (would calculate from recent predictions vs outcomes)
        accuracy = self._calculate_model_accuracy()
        if accuracy is not None:
            metrics_collector.record_model_health("prediction_accuracy", accuracy)
        
        # Model drift
        drift = self._calculate_model_drift()
        if drift is not None:
            metrics_collector.record_model_health("model_drift", drift)
        
        # Prediction latency
        latency = self._measure_prediction_latency()
        if latency is not None:
            metrics_collector.record_model_health("prediction_latency", latency)
    
    def _collect_execution_metrics(self):
        """Collect execution quality metrics"""
        # Fill ratio
        fill_ratio = self._calculate_fill_ratio()
        if fill_ratio is not None:
            metrics_collector.record_execution_quality("fill_ratio", fill_ratio)
        
        # Slippage
        slippage = self._calculate_slippage()
        if slippage is not None:
            metrics_collector.record_execution_quality("slippage", slippage)
        
        # Order latency
        order_latency = self._measure_order_latency()
        if order_latency is not None:
            metrics_collector.record_execution_quality("order_latency", order_latency)
    
    def _collect_job_metrics(self):
        """Collect job reliability metrics"""
        # Get critical jobs list from kill switch config
        critical_jobs = ["poll_prices", "process_events", "generate_predictions", "execute_trades"]
        
        for job_name in critical_jobs:
            # Job uptime (simplified - would track actual job status)
            uptime = self._calculate_job_uptime(job_name)
            if uptime is not None:
                metrics_collector.record_job_reliability(job_name, "uptime", uptime)
            
            # Heartbeat freshness
            heartbeat_age = self._get_job_heartbeat_age(job_name)
            if heartbeat_age is not None:
                metrics_collector.record_job_reliability(job_name, "heartbeat_age", heartbeat_age)
    
    def _collect_system_health_metrics(self):
        """Collect system-wide health metrics"""
        # CPU usage
        cpu_usage = self._get_cpu_usage()
        if cpu_usage is not None:
            metrics_collector.record_system_health("cpu_usage", cpu_usage)
        
        # Memory usage
        memory_usage = self._get_memory_usage()
        if memory_usage is not None:
            metrics_collector.record_system_health("memory_usage", memory_usage)
        
        # Disk usage
        disk_usage = self._get_disk_usage()
        if disk_usage is not None:
            metrics_collector.record_system_health("disk_usage", disk_usage)
    
    def _evaluate_slos(self) -> List[str]:
        """Evaluate all SLOs and return violations"""
        violations = []
        
        for category, slo_dict in slo_evaluator.get_all_slos().items():
            for slo_name, slo_def in slo_dict.items():
                latest_value = metrics_collector.get_latest_value(slo_def.name)
                
                if latest_value is not None:
                    status = slo_evaluator.evaluate_metric(slo_def, latest_value)
                    
                    if status in [SLOStatus.WARNING, SLOStatus.CRITICAL]:
                        violations.append(f"{category}.{slo_name}: {status.value}")
                        
                        # Record SLO status as metric
                        metrics_collector.record_system_health(f"slo.{slo_def.name}", 
                                                            1 if status == SLOStatus.CRITICAL else 0.5)
        
        return violations
    
    def _check_alerts(self) -> List[str]:
        """Check for new alerts based on current metrics"""
        new_alert_ids = []
        
        # Check all SLO metrics for alerts
        for category, slo_dict in slo_evaluator.get_all_slos().items():
            for slo_name, slo_def in slo_dict.items():
                latest_value = metrics_collector.get_latest_value(slo_def.name)
                
                if latest_value is not None:
                    alerts = alert_manager.check_metric_alerts(slo_def.name, latest_value)
                    
                    for alert in alerts:
                        new_alert_ids.append(alert.id)
                        
                        # Trigger escalation if needed
                        if alert.severity == AlertSeverity.CRITICAL:
                            escalation_ids = escalation_manager.check_escalation_triggers(alert.id)
        
        return new_alert_ids
    
    def _process_auto_remediation(self, alert_ids: List[str]):
        """Process auto-remediation for new alerts"""
        for alert_id in alert_ids:
            try:
                remediation_id = auto_remediation.trigger_remediation(alert_id)
                if remediation_id:
                    print(f"Triggered auto-remediation {remediation_id} for alert {alert_id}")
            except Exception as e:
                print(f"Failed to trigger auto-remediation for alert {alert_id}: {e}")
    
    def _check_kill_switch_triggers(self) -> List[str]:
        """Check and execute kill switch triggers"""
        try:
            return kill_switch_integration.check_triggers()
        except Exception as e:
            print(f"Error checking kill switch triggers: {e}")
            return []
    
    def _process_escalations(self, alert_ids: List[str]):
        """Process escalations for alerts"""
        for alert_id in alert_ids:
            try:
                escalation_ids = escalation_manager.check_escalation_triggers(alert_id)
                if escalation_ids:
                    print(f"Triggered escalations {escalation_ids} for alert {alert_id}")
            except Exception as e:
                print(f"Failed to process escalation for alert {alert_id}: {e}")
    
    def _record_monitoring_cycle(self, start_ms: int, slo_violations: List[str], 
                               new_alerts: List[str], ks_triggers: List[str]):
        """Record monitoring cycle completion"""
        try:
            from engine.storage import connect
            with connect() as con:
                con.execute("""
                    INSERT OR REPLACE INTO monitoring_cycles 
                    (cycle_start_ms, cycle_duration_ms, slo_violations_count, 
                     new_alerts_count, kill_switch_triggers_count, cycle_data_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    start_ms,
                    int(time.time() * 1000) - start_ms,
                    len(slo_violations),
                    len(new_alerts),
                    len(ks_triggers),
                    json.dumps({
                        "slo_violations": slo_violations,
                        "new_alerts": new_alerts,
                        "kill_switch_triggers": ks_triggers
                    }, separators=(',', ':'))
                ))
        except Exception as e:
            print(f"Error recording monitoring cycle: {e}")
    
    # Helper methods for metric collection (simplified implementations)
    def _get_latest_data_age(self, table_name: str) -> Optional[float]:
        """Get age of latest data in seconds"""
        try:
            from engine.storage import connect
            with connect() as con:
                row = con.execute(f"SELECT MAX(ts_ms) FROM {table_name}").fetchone()
                if row and row[0]:
                    latest_ms = row[0]
                    return (time.time() * 1000 - latest_ms) / 1000
        except Exception:
            pass
        return None
    
    def _calculate_model_accuracy(self) -> Optional[float]:
        """Calculate model accuracy (simplified)"""
        # Would implement actual accuracy calculation
        return 85.0  # Placeholder
    
    def _calculate_model_drift(self) -> Optional[float]:
        """Calculate model drift (simplified)"""
        # Would implement actual drift calculation
        return 0.1  # Placeholder
    
    def _measure_prediction_latency(self) -> Optional[float]:
        """Measure prediction latency (simplified)"""
        # Would implement actual latency measurement
        return 2.5  # Placeholder
    
    def _calculate_fill_ratio(self) -> Optional[float]:
        """Calculate order fill ratio (simplified)"""
        # Would implement actual fill ratio calculation
        return 92.0  # Placeholder
    
    def _calculate_slippage(self) -> Optional[float]:
        """Calculate execution slippage (simplified)"""
        # Would implement actual slippage calculation
        return 3.2  # Placeholder
    
    def _measure_order_latency(self) -> Optional[float]:
        """Measure order execution latency (simplified)"""
        # Would implement actual latency measurement
        return 1.8  # Placeholder
    
    def _calculate_job_uptime(self, job_name: str) -> Optional[float]:
        """Calculate job uptime percentage (simplified)"""
        # Would implement actual uptime calculation
        return 98.5  # Placeholder
    
    def _get_job_heartbeat_age(self, job_name: str) -> Optional[float]:
        """Get job heartbeat age in seconds"""
        try:
            from engine.storage import connect
            with connect() as con:
                row = con.execute(
                    "SELECT ts_ms FROM job_heartbeats WHERE job_name = ?",
                    (job_name,)
                ).fetchone()
                if row and row[0]:
                    return (time.time() * 1000 - row[0]) / 1000
        except Exception:
            pass
        return None
    
    def _get_cpu_usage(self) -> Optional[float]:
        """Get CPU usage percentage"""
        try:
            import psutil
            return psutil.cpu_percent(interval=None)
        except Exception:
            return None
    
    def _get_memory_usage(self) -> Optional[float]:
        """Get memory usage percentage"""
        try:
            import psutil
            return psutil.virtual_memory().percent
        except Exception:
            return None
    
    def _get_disk_usage(self) -> Optional[float]:
        """Get disk usage percentage"""
        try:
            import psutil
            return psutil.disk_usage('/').percent
        except Exception:
            return None
    
    def get_status(self) -> Dict[str, Any]:
        """Get orchestrator status"""
        return {
            "running": self.running,
            "last_check_ms": self.last_check_ms,
            "check_interval_seconds": self.check_interval_seconds,
            "component_status": self.component_status.copy()
        }

# Global monitoring orchestrator instance
monitoring_orchestrator = MonitoringOrchestrator()
