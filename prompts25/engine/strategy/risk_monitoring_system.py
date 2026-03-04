"""
Risk Monitoring and Alerting System

Provides real-time monitoring and alerting for:
- Correlation risk spikes
- Crowding violations
- Concentration risk breaches
- System performance issues
- Market regime changes
"""

import numpy as np
from typing import Dict, List, Tuple, Optional, Any, Callable
from dataclasses import dataclass, field
from collections import defaultdict, deque
import time
import json
import threading
from enum import Enum

from engine.storage import connect
from engine.strategy.enhanced_correlation_analyzer import EnhancedCorrelationAnalyzer
from engine.strategy.crowding_penalty_system import CrowdingPenaltySystem
from engine.strategy.concentration_risk_controller import ConcentrationRiskController, RiskLevel


class AlertSeverity(Enum):
    """Alert severity levels"""
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AlertCategory(Enum):
    """Alert categories"""
    CORRELATION_RISK = "correlation_risk"
    CROWDING_RISK = "crowding_risk"
    CONCENTRATION_RISK = "concentration_risk"
    PERFORMANCE = "performance"
    SYSTEM = "system"
    MARKET_REGIME = "market_regime"


@dataclass
class Alert:
    """Risk alert data structure"""
    id: str
    category: AlertCategory
    severity: AlertSeverity
    title: str
    description: str
    timestamp: float
    source: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    threshold_value: float = 0.0
    current_value: float = 0.0
    recommended_action: str = ""
    is_resolved: bool = False
    resolution_timestamp: Optional[float] = None
    resolution_note: str = ""


@dataclass
class MonitoringConfig:
    """Monitoring system configuration"""
    # Alert thresholds
    correlation_risk_threshold: float = 0.7
    crowding_score_threshold: float = 0.5
    concentration_risk_threshold: float = 0.4
    diversification_min_threshold: float = 0.5
    
    # Performance thresholds
    max_allocation_latency_ms: float = 1000.0
    max_penalty_processing_time_ms: float = 500.0
    max_memory_usage_mb: float = 1024.0
    
    # Alert settings
    enable_email_alerts: bool = False
    email_recipients: List[str] = field(default_factory=list)
    smtp_server: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    
    # Monitoring frequency
    monitoring_interval_s: int = 60
    alert_cooldown_s: int = 300  # 5 minutes between similar alerts
    max_alerts_per_hour: int = 50
    
    # Data retention
    alert_history_days: int = 30
    metrics_history_hours: int = 24


class RiskMonitoringSystem:
    """
    Comprehensive risk monitoring and alerting system.
    """
    
    def __init__(self, config: MonitoringConfig = None):
        self.config = config or MonitoringConfig()
        self.alerts: List[Alert] = []
        self.alert_history: List[Alert] = []
        self.metrics_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1440))  # 24 hours at 1/min
        
        # External systems to monitor
        self.correlation_analyzer: Optional[EnhancedCorrelationAnalyzer] = None
        self.crowding_penalty_system: Optional[CrowdingPenaltySystem] = None
        self.concentration_controller: Optional[ConcentrationRiskController] = None
        
        # Monitoring state
        self.is_monitoring = False
        self.monitoring_thread: Optional[threading.Thread] = None
        self.last_monitoring_ts = 0
        self.alert_cooldowns: Dict[str, float] = {}
        self.hourly_alert_count = 0
        self.last_hour_reset_ts = time.time()
        
        # Performance tracking
        self.performance_metrics: Dict[str, List[float]] = defaultdict(list)
        
        # Alert callbacks
        self.alert_callbacks: List[Callable[[Alert], None]] = []
        
    def register_systems(self, correlation_analyzer: EnhancedCorrelationAnalyzer,
                        crowding_penalty_system: CrowdingPenaltySystem,
                        concentration_controller: ConcentrationRiskController) -> None:
        """Register external systems for monitoring"""
        self.correlation_analyzer = correlation_analyzer
        self.crowding_penalty_system = crowding_penalty_system
        self.concentration_controller = concentration_controller
    
    def add_alert_callback(self, callback: Callable[[Alert], None]) -> None:
        """Add callback function for alert notifications"""
        self.alert_callbacks.append(callback)
    
    def start_monitoring(self) -> None:
        """Start the monitoring system"""
        if self.is_monitoring:
            return
        
        self.is_monitoring = True
        self.monitoring_thread = threading.Thread(target=self._monitoring_loop, daemon=True)
        self.monitoring_thread.start()
        print("Risk monitoring system started")
    
    def stop_monitoring(self) -> None:
        """Stop the monitoring system"""
        self.is_monitoring = False
        if self.monitoring_thread:
            self.monitoring_thread.join(timeout=5)
        print("Risk monitoring system stopped")
    
    def _monitoring_loop(self) -> None:
        """Main monitoring loop"""
        while self.is_monitoring:
            try:
                self._perform_monitoring_cycle()
                time.sleep(self.config.monitoring_interval_s)
            except Exception as e:
                print(f"Error in monitoring loop: {e}")
                time.sleep(10)  # Wait before retrying
    
    def _perform_monitoring_cycle(self) -> None:
        """Perform one monitoring cycle"""
        current_time = time.time()
        
        # Reset hourly alert count if needed
        if current_time - self.last_hour_reset_ts > 3600:
            self.hourly_alert_count = 0
            self.last_hour_reset_ts = current_time
        
        # Check if we've exceeded alert rate limit
        if self.hourly_alert_count >= self.config.max_alerts_per_hour:
            return
        
        # Monitor correlation risk
        self._monitor_correlation_risk()
        
        # Monitor crowding risk
        self._monitor_crowding_risk()
        
        # Monitor concentration risk
        self._monitor_concentration_risk()
        
        # Monitor system performance
        self._monitor_system_performance()
        
        # Clean up old alerts
        self._cleanup_old_alerts()
        
        self.last_monitoring_ts = current_time
    
    def _monitor_correlation_risk(self) -> None:
        """Monitor correlation risk metrics"""
        if not self.correlation_analyzer:
            return
        
        try:
            risk_report = self.correlation_analyzer.get_risk_report()
            
            # Check average correlation
            avg_correlation = risk_report.get('avg_correlation', 0)
            if avg_correlation > self.config.correlation_risk_threshold:
                self._create_alert(
                    category=AlertCategory.CORRELATION_RISK,
                    severity=AlertSeverity.HIGH if avg_correlation > 0.8 else AlertSeverity.MEDIUM,
                    title="High Portfolio Correlation Detected",
                    description=f"Average correlation: {avg_correlation:.3f}",
                    metrics={'avg_correlation': avg_correlation, 'max_correlation': risk_report.get('max_correlation', 0)},
                    threshold_value=self.config.correlation_risk_threshold,
                    current_value=avg_correlation,
                    recommended_action="Consider reducing exposure to correlated strategies"
                )
            
            # Store metrics
            self.metrics_history['avg_correlation'].append(avg_correlation)
            self.metrics_history['max_correlation'].append(risk_report.get('max_correlation', 0))
            
        except Exception as e:
            print(f"Error monitoring correlation risk: {e}")
    
    def _monitor_crowding_risk(self) -> None:
        """Monitor crowding risk metrics"""
        if not self.crowding_penalty_system:
            return
        
        try:
            penalty_stats = self.crowding_penalty_system.get_penalty_statistics()
            high_risk_models = self.crowding_penalty_system.get_high_risk_models()
            
            # Check average crowding penalty
            avg_penalty = penalty_stats.get('total_penalties_avg', 0)
            if avg_penalty > self.config.crowding_score_threshold:
                self._create_alert(
                    category=AlertCategory.CROWDING_RISK,
                    severity=AlertSeverity.HIGH if avg_penalty > 0.7 else AlertSeverity.MEDIUM,
                    title="High Crowding Risk Detected",
                    description=f"Average crowding penalty: {avg_penalty:.3f}",
                    metrics={
                        'avg_penalty': avg_penalty,
                        'max_penalty': penalty_stats.get('total_penalties_max', 0),
                        'high_risk_models_count': len(high_risk_models)
                    },
                    threshold_value=self.config.crowding_score_threshold,
                    current_value=avg_penalty,
                    recommended_action="Reduce exposure to crowded assets/strategies"
                )
            
            # Check for individual high-risk models
            for model in high_risk_models[:5]:  # Top 5
                crowding_score = model.get('crowding_score', 0)
                if crowding_score > 0.6:
                    self._create_alert(
                        category=AlertCategory.CROWDING_RISK,
                        severity=AlertSeverity.CRITICAL if crowding_score > 0.8 else AlertSeverity.HIGH,
                        title=f"High Crowding Risk: {model.get('model_id', 'Unknown')}",
                        description=f"Model crowding score: {crowding_score:.3f}",
                        metrics={'model_id': model.get('model_id'), 'crowding_score': crowding_score},
                        threshold_value=0.6,
                        current_value=crowding_score,
                        recommended_action="Immediately reduce position size in this model"
                    )
            
            # Store metrics
            self.metrics_history['avg_crowding_penalty'].append(avg_penalty)
            self.metrics_history['high_risk_models_count'].append(len(high_risk_models))
            
        except Exception as e:
            print(f"Error monitoring crowding risk: {e}")
    
    def _monitor_concentration_risk(self) -> None:
        """Monitor concentration risk metrics"""
        if not self.concentration_controller:
            return
        
        try:
            risk_summary = self.concentration_controller.get_risk_summary()
            concentration_metrics = risk_summary.get('concentration_metrics', {})
            
            # Check diversification score
            diversification_score = concentration_metrics.get('diversification_score', 1.0)
            if diversification_score < self.config.diversification_min_threshold:
                self._create_alert(
                    category=AlertCategory.CONCENTRATION_RISK,
                    severity=AlertSeverity.HIGH if diversification_score < 0.3 else AlertSeverity.MEDIUM,
                    title="Low Portfolio Diversification",
                    description=f"Diversification score: {diversification_score:.3f}",
                    metrics={
                        'diversification_score': diversification_score,
                        'total_positions': concentration_metrics.get('total_positions', 0),
                        'risk_level': concentration_metrics.get('risk_level', 'unknown')
                    },
                    threshold_value=self.config.diversification_min_threshold,
                    current_value=diversification_score,
                    recommended_action="Add positions in underrepresented sectors/assets"
                )
            
            # Check for critical concentration alerts
            active_alerts = risk_summary.get('active_alerts', [])
            critical_alerts = [alert for alert in active_alerts if alert.get('severity') in ['high', 'critical']]
            
            if critical_alerts:
                for alert in critical_alerts[:3]:  # Top 3 critical alerts
                    self._create_alert(
                        category=AlertCategory.CONCENTRATION_RISK,
                        severity=AlertSeverity.CRITICAL if alert.get('severity') == 'critical' else AlertSeverity.HIGH,
                        title=f"Concentration Risk: {alert.get('type', 'Unknown')}",
                        description=alert.get('description', ''),
                        metrics=alert,
                        threshold_value=alert.get('limit_value', 0),
                        current_value=alert.get('current_value', 0),
                        recommended_action=alert.get('recommended_action', '')
                    )
            
            # Store metrics
            self.metrics_history['diversification_score'].append(diversification_score)
            self.metrics_history['active_concentration_alerts'].append(len(active_alerts))
            
        except Exception as e:
            print(f"Error monitoring concentration risk: {e}")
    
    def _monitor_system_performance(self) -> None:
        """Monitor system performance metrics"""
        try:
            # Check memory usage (simplified)
            import psutil
            memory_usage_mb = psutil.virtual_memory().used / (1024 * 1024)
            
            if memory_usage_mb > self.config.max_memory_usage_mb:
                self._create_alert(
                    category=AlertCategory.SYSTEM,
                    severity=AlertSeverity.HIGH,
                    title="High Memory Usage",
                    description=f"Memory usage: {memory_usage_mb:.1f} MB",
                    metrics={'memory_usage_mb': memory_usage_mb},
                    threshold_value=self.config.max_memory_usage_mb,
                    current_value=memory_usage_mb,
                    recommended_action="Restart system or investigate memory leaks"
                )
            
            # Store metrics
            self.metrics_history['memory_usage_mb'].append(memory_usage_mb)
            
        except Exception as e:
            print(f"Error monitoring system performance: {e}")
    
    def _create_alert(self, category: AlertCategory, severity: AlertSeverity,
                     title: str, description: str, metrics: Dict[str, Any],
                     threshold_value: float, current_value: float,
                     recommended_action: str) -> None:
        """Create and process a new alert"""
        # Check cooldown
        alert_key = f"{category.value}_{title}"
        current_time = time.time()
        
        if alert_key in self.alert_cooldowns:
            if current_time - self.alert_cooldowns[alert_key] < self.config.alert_cooldown_s:
                return  # Still in cooldown
        
        # Create alert
        alert = Alert(
            id=f"alert_{int(current_time * 1000)}_{len(self.alerts)}",
            category=category,
            severity=severity,
            title=title,
            description=description,
            timestamp=current_time,
            source="risk_monitoring_system",
            metrics=metrics,
            threshold_value=threshold_value,
            current_value=current_value,
            recommended_action=recommended_action
        )
        
        # Add to active alerts
        self.alerts.append(alert)
        self.alert_history.append(alert)
        
        # Update cooldown
        self.alert_cooldowns[alert_key] = current_time
        self.hourly_alert_count += 1
        
        # Trigger callbacks
        for callback in self.alert_callbacks:
            try:
                callback(alert)
            except Exception as e:
                print(f"Error in alert callback: {e}")
        
        # Send email if configured
        if self.config.enable_email_alerts and severity in [AlertSeverity.HIGH, AlertSeverity.CRITICAL]:
            self._send_email_alert(alert)
        
        print(f"ALERT [{severity.value.upper()}] {title}: {description}")
    
    def _send_email_alert(self, alert: Alert) -> None:
        """Send email alert (simplified version)"""
        if not self.config.email_recipients:
            return
        
        # For now, just log the alert - email functionality can be added later
        print(f"EMAIL ALERT: [{alert.severity.value.upper()}] {alert.title}")
        print(f"Recipients: {', '.join(self.config.email_recipients)}")
        print(f"Description: {alert.description}")
        print(f"Recommended Action: {alert.recommended_action}")
    
    def _cleanup_old_alerts(self) -> None:
        """Clean up old alerts and history"""
        current_time = time.time()
        max_age_seconds = self.config.alert_history_days * 24 * 3600
        
        # Remove old alerts from active list
        self.alerts = [alert for alert in self.alerts if current_time - alert.timestamp < max_age_seconds]
        
        # Trim history if needed
        if len(self.alert_history) > 10000:
            self.alert_history = self.alert_history[-5000:]
        
        # Clean up old cooldowns
        self.alert_cooldowns = {
            key: ts for key, ts in self.alert_cooldowns.items()
            if current_time - ts < self.config.alert_cooldown_s
        }
    
    def record_performance_metric(self, metric_name: str, value: float) -> None:
        """Record a performance metric"""
        self.performance_metrics[metric_name].append(value)
        
        # Keep only last 1000 values
        if len(self.performance_metrics[metric_name]) > 1000:
            self.performance_metrics[metric_name] = self.performance_metrics[metric_name][-1000:]
        
        # Check for performance issues
        if metric_name == 'allocation_latency_ms' and value > self.config.max_allocation_latency_ms:
            self._create_alert(
                category=AlertCategory.PERFORMANCE,
                severity=AlertSeverity.MEDIUM,
                title="High Allocation Latency",
                description=f"Allocation latency: {value:.1f}ms",
                metrics={'latency_ms': value},
                threshold_value=self.config.max_allocation_latency_ms,
                current_value=value,
                recommended_action="Investigate allocation performance bottleneck"
            )
    
    def get_active_alerts(self, severity: Optional[AlertSeverity] = None,
                        category: Optional[AlertCategory] = None) -> List[Alert]:
        """Get active alerts, optionally filtered"""
        alerts = self.alerts
        
        if severity:
            alerts = [alert for alert in alerts if alert.severity == severity]
        
        if category:
            alerts = [alert for alert in alerts if alert.category == category]
        
        return sorted(alerts, key=lambda x: x.timestamp, reverse=True)
    
    def get_metrics_summary(self) -> Dict[str, Any]:
        """Get summary of monitoring metrics"""
        current_time = time.time()
        
        summary = {
            'monitoring_status': 'active' if self.is_monitoring else 'inactive',
            'last_monitoring_ts': self.last_monitoring_ts,
            'active_alerts_count': len(self.alerts),
            'hourly_alert_count': self.hourly_alert_count,
            'total_alerts_count': len(self.alert_history),
            'alert_breakdown': {
                'by_severity': defaultdict(int),
                'by_category': defaultdict(int)
            },
            'current_metrics': {},
            'performance_metrics': {}
        }
        
        # Alert breakdown
        for alert in self.alerts:
            summary['alert_breakdown']['by_severity'][alert.severity.value] += 1
            summary['alert_breakdown']['by_category'][alert.category.value] += 1
        
        # Current metrics (latest values)
        for metric_name, values in self.metrics_history.items():
            if values:
                summary['current_metrics'][metric_name] = values[-1]
        
        # Performance metrics (latest averages)
        for metric_name, values in self.performance_metrics.items():
            if values:
                summary['performance_metrics'][metric_name] = {
                    'latest': values[-1],
                    'avg_10': np.mean(values[-10:]) if len(values) >= 10 else np.mean(values),
                    'avg_100': np.mean(values[-100:]) if len(values) >= 100 else np.mean(values)
                }
        
        return summary
    
    def resolve_alert(self, alert_id: str, resolution_note: str = "") -> bool:
        """Resolve an alert"""
        for alert in self.alerts:
            if alert.id == alert_id:
                alert.is_resolved = True
                alert.resolution_timestamp = time.time()
                alert.resolution_note = resolution_note
                return True
        return False
    
    def export_alerts(self, start_time: Optional[float] = None,
                     end_time: Optional[float] = None,
                     severity: Optional[AlertSeverity] = None) -> List[Dict]:
        """Export alerts for analysis"""
        alerts = self.alert_history
        
        if start_time:
            alerts = [alert for alert in alerts if alert.timestamp >= start_time]
        
        if end_time:
            alerts = [alert for alert in alerts if alert.timestamp <= end_time]
        
        if severity:
            alerts = [alert for alert in alerts if alert.severity == severity]
        
        return [
            {
                'id': alert.id,
                'category': alert.category.value,
                'severity': alert.severity.value,
                'title': alert.title,
                'description': alert.description,
                'timestamp': alert.timestamp,
                'metrics': alert.metrics,
                'threshold_value': alert.threshold_value,
                'current_value': alert.current_value,
                'recommended_action': alert.recommended_action,
                'is_resolved': alert.is_resolved,
                'resolution_timestamp': alert.resolution_timestamp,
                'resolution_note': alert.resolution_note
            }
            for alert in alerts
        ]
