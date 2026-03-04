"""
Feedback Signals and Monitoring System

Comprehensive monitoring system for research budget allocation with real-time feedback signals,
performance tracking, anomaly detection, and alerting mechanisms.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum
import logging
from datetime import datetime, timedelta
from collections import deque
import json
from scipy import stats
from research_budget_allocator import ResearchAsset, ResearchStatus

logger = logging.getLogger(__name__)

class SignalType(Enum):
    PERFORMANCE = "performance"
    BUDGET_UTILIZATION = "budget_utilization"
    EFFICIENCY = "efficiency"
    CONVERGENCE = "convergence"
    ANOMALY = "anomaly"
    OPPORTUNITY = "opportunity"
    RISK = "risk"

class AlertLevel(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"

class MetricType(Enum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    TIMER = "timer"

@dataclass
class FeedbackSignal:
    """Represents a feedback signal with metadata"""
    signal_type: SignalType
    asset_id: str
    value: float
    threshold: float
    current_value: float
    message: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict = field(default_factory=dict)

@dataclass
class Alert:
    """Represents an alert with severity and context"""
    alert_id: str
    level: AlertLevel
    title: str
    message: str
    asset_id: Optional[str]
    signal_type: SignalType
    value: float
    threshold: float
    timestamp: datetime = field(default_factory=datetime.now)
    acknowledged: bool = False
    resolved: bool = False
    metadata: Dict = field(default_factory=dict)

@dataclass
class Metric:
    """Represents a monitored metric"""
    name: str
    metric_type: MetricType
    value: float
    labels: Dict[str, str] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    unit: str = ""

class FeedbackMonitor:
    """Main feedback monitoring system"""
    
    def __init__(self, window_size: int = 100, alert_threshold: float = 2.0):
        self.window_size = window_size
        self.alert_threshold = alert_threshold
        
        # Signal storage
        self.signals: deque = deque(maxlen=window_size)
        self.alerts: List[Alert] = []
        self.metrics: List[Metric] = []
        
        # Asset-specific tracking
        self.asset_signals: Dict[str, deque] = {}
        self.asset_metrics: Dict[str, Dict[str, List[float]]] = {}
        self.asset_baselines: Dict[str, Dict[str, float]] = {}
        
        # Monitoring configuration
        self.signal_handlers: Dict[SignalType, List[Callable]] = {}
        self.thresholds: Dict[str, Dict[str, float]] = {}
        
        # Anomaly detection
        self.anomaly_detectors: Dict[str, Callable] = {}
        self.baseline_window = 50  # Window for baseline calculation
        
        # Performance tracking
        self.performance_history: Dict[str, List[Tuple[datetime, float]]] = {}
        self.budget_utilization_history: Dict[str, List[Tuple[datetime, float]]] = {}
        
        # Initialize default thresholds
        self._initialize_thresholds()
        
        logger.info("Initialized FeedbackMonitor")
    
    def _initialize_thresholds(self):
        """Initialize default monitoring thresholds"""
        self.thresholds = {
            "performance": {
                "min_acceptable": 0.3,
                "target": 0.7,
                "excellent": 0.9,
                "decline_rate": -0.1
            },
            "budget_utilization": {
                "low_threshold": 0.1,  # 10% utilization is too low
                "optimal_min": 0.3,     # 30% is minimum optimal
                "optimal_max": 0.8,     # 80% is maximum optimal
                "high_threshold": 0.95   # 95% is too high (risk of exhaustion)
            },
            "efficiency": {
                "min_roi": 0.1,         # 10% minimum ROI
                "target_roi": 0.5,       # 50% target ROI
                "compute_efficiency": 0.8 # 80% compute efficiency target
            },
            "convergence": {
                "min_improvement_rate": 0.01,  # 1% minimum improvement
                "stagnation_periods": 10,       # 10 periods of no improvement
                "convergence_threshold": 0.001   # 0.1% change indicates convergence
            },
            "risk": {
                "max_volatility": 0.5,     # 50% max volatility
                "max_drawdown": 0.3,        # 30% max drawdown
                "correlation_threshold": 0.8 # 80% correlation threshold
            }
        }
    
    def register_signal_handler(self, signal_type: SignalType, handler: Callable):
        """Register a custom signal handler"""
        if signal_type not in self.signal_handlers:
            self.signal_handlers[signal_type] = []
        self.signal_handlers[signal_type].append(handler)
        logger.info(f"Registered handler for {signal_type.value} signals")
    
    def register_anomaly_detector(self, metric_name: str, detector: Callable):
        """Register a custom anomaly detector"""
        self.anomaly_detectors[metric_name] = detector
        logger.info(f"Registered anomaly detector for {metric_name}")
    
    def update_threshold(self, category: str, metric: str, value: float):
        """Update monitoring threshold"""
        if category in self.thresholds:
            self.thresholds[category][metric] = value
            logger.info(f"Updated threshold: {category}.{metric} = {value}")
        else:
            logger.warning(f"Threshold category {category} not found")
    
    def process_asset_update(self, asset: ResearchAsset):
        """Process asset update and generate feedback signals"""
        asset_id = asset.id
        
        # Initialize asset tracking if needed
        if asset_id not in self.asset_signals:
            self.asset_signals[asset_id] = deque(maxlen=self.window_size)
            self.asset_metrics[asset_id] = {}
            self.asset_baselines[asset_id] = {}
            self.performance_history[asset_id] = []
            self.budget_utilization_history[asset_id] = []
        
        # Generate performance signals
        self._process_performance_signals(asset)
        
        # Generate budget utilization signals
        self._process_budget_signals(asset)
        
        # Generate efficiency signals
        self._process_efficiency_signals(asset)
        
        # Generate convergence signals
        self._process_convergence_signals(asset)
        
        # Check for anomalies
        self._check_anomalies(asset)
        
        # Update metrics
        self._update_metrics(asset)
    
    def _process_performance_signals(self, asset: ResearchAsset):
        """Process performance-related signals"""
        asset_id = asset.id
        
        if not asset.recent_performance:
            return
        
        # Current performance
        current_performance = asset.recent_performance[-1]
        
        # Performance trend
        if len(asset.recent_performance) >= 5:
            recent_performances = asset.recent_performance[-5:]
            trend = np.polyfit(range(len(recent_performances)), recent_performances, 1)[0]
            
            # Decline detection
            if trend < self.thresholds["performance"]["decline_rate"]:
                signal = FeedbackSignal(
                    signal_type=SignalType.PERFORMANCE,
                    asset_id=asset_id,
                    value=trend,
                    threshold=self.thresholds["performance"]["decline_rate"],
                    current_value=current_performance,
                    message=f"Performance declining at rate {trend:.3f}",
                    metadata={"trend": trend, "recent_performances": recent_performances}
                )
                self._add_signal(signal)
                
                # Generate alert if critical
                if trend < self.thresholds["performance"]["decline_rate"] * 2:
                    self._create_alert(
                        AlertLevel.WARNING,
                        "Performance Decline",
                        f"Asset {asset.name} showing significant performance decline",
                        asset_id,
                        SignalType.PERFORMANCE,
                        trend,
                        self.thresholds["performance"]["decline_rate"] * 2
                    )
        
        # Low performance detection
        if current_performance < self.thresholds["performance"]["min_acceptable"]:
            signal = FeedbackSignal(
                signal_type=SignalType.PERFORMANCE,
                asset_id=asset_id,
                value=current_performance,
                threshold=self.thresholds["performance"]["min_acceptable"],
                current_value=current_performance,
                message=f"Performance below acceptable threshold",
                metadata={"performance": current_performance}
            )
            self._add_signal(signal)
            
            if current_performance < self.thresholds["performance"]["min_acceptable"] * 0.5:
                self._create_alert(
                    AlertLevel.CRITICAL,
                    "Low Performance",
                    f"Asset {asset.name} performance critically low",
                    asset_id,
                    SignalType.PERFORMANCE,
                    current_performance,
                    self.thresholds["performance"]["min_acceptable"] * 0.5
                )
        
        # Record performance history
        self.performance_history[asset_id].append((datetime.now(), current_performance))
        if len(self.performance_history[asset_id]) > self.window_size:
            self.performance_history[asset_id].pop(0)
    
    def _process_budget_signals(self, asset: ResearchAsset):
        """Process budget utilization signals"""
        asset_id = asset.id
        
        # Calculate utilization ratio
        if asset.initial_budget > 0:
            utilization = (asset.initial_budget - asset.current_budget) / asset.initial_budget
        else:
            utilization = 0
        
        # Record utilization history
        self.budget_utilization_history[asset_id].append((datetime.now(), utilization))
        if len(self.budget_utilization_history[asset_id]) > self.window_size:
            self.budget_utilization_history[asset_id].pop(0)
        
        # Low utilization alert
        if utilization < self.thresholds["budget_utilization"]["low_threshold"]:
            signal = FeedbackSignal(
                signal_type=SignalType.BUDGET_UTILIZATION,
                asset_id=asset_id,
                value=utilization,
                threshold=self.thresholds["budget_utilization"]["low_threshold"],
                current_value=utilization,
                message=f"Low budget utilization: {utilization:.1%}",
                metadata={"utilization": utilization, "remaining": asset.current_budget}
            )
            self._add_signal(signal)
        
        # High utilization alert
        if utilization > self.thresholds["budget_utilization"]["high_threshold"]:
            signal = FeedbackSignal(
                signal_type=SignalType.BUDGET_UTILIZATION,
                asset_id=asset_id,
                value=utilization,
                threshold=self.thresholds["budget_utilization"]["high_threshold"],
                current_value=utilization,
                message=f"High budget utilization: {utilization:.1%}",
                metadata={"utilization": utilization, "remaining": asset.current_budget}
            )
            self._add_signal(signal)
            
            self._create_alert(
                AlertLevel.WARNING,
                "High Budget Utilization",
                f"Asset {asset.name} approaching budget exhaustion",
                asset_id,
                SignalType.BUDGET_UTILIZATION,
                utilization,
                self.thresholds["budget_utilization"]["high_threshold"]
            )
    
    def _process_efficiency_signals(self, asset: ResearchAsset):
        """Process efficiency-related signals"""
        asset_id = asset.id
        
        # ROI calculation
        if asset.total_spent > 0 and asset.expected_payoff > 0:
            roi = asset.expected_payoff / (asset.total_spent / 1000)  # Performance per $1000
            
            if roi < self.thresholds["efficiency"]["min_roi"]:
                signal = FeedbackSignal(
                    signal_type=SignalType.EFFICIENCY,
                    asset_id=asset_id,
                    value=roi,
                    threshold=self.thresholds["efficiency"]["min_roi"],
                    current_value=roi,
                    message=f"Low ROI: {roi:.3f}",
                    metadata={"roi": roi, "spent": asset.total_spent, "payoff": asset.expected_payoff}
                )
                self._add_signal(signal)
        
        # Compute efficiency
        if asset.compute_hours_used > 0:
            compute_efficiency = asset.expected_payoff / (asset.compute_hours_used + 1e-6)
            
            if compute_efficiency < self.thresholds["efficiency"]["compute_efficiency"]:
                signal = FeedbackSignal(
                    signal_type=SignalType.EFFICIENCY,
                    asset_id=asset_id,
                    value=compute_efficiency,
                    threshold=self.thresholds["efficiency"]["compute_efficiency"],
                    current_value=compute_efficiency,
                    message=f"Low compute efficiency: {compute_efficiency:.3f}",
                    metadata={"compute_efficiency": compute_efficiency, "compute_hours": asset.compute_hours_used}
                )
                self._add_signal(signal)
    
    def _process_convergence_signals(self, asset: ResearchAsset):
        """Process convergence-related signals"""
        asset_id = asset.id
        
        if len(asset.recent_performance) < 5:
            return
        
        recent_performances = asset.recent_performance[-5:]
        
        # Calculate improvement rate
        improvements = []
        for i in range(1, len(recent_performances)):
            if recent_performances[i-1] > 0:
                improvement = (recent_performances[i] - recent_performances[i-1]) / recent_performances[i-1]
                improvements.append(improvement)
        
        if improvements:
            avg_improvement = np.mean(improvements)
            
            # Stagnation detection
            if avg_improvement < self.thresholds["convergence"]["min_improvement_rate"]:
                signal = FeedbackSignal(
                    signal_type=SignalType.CONVERGENCE,
                    asset_id=asset_id,
                    value=avg_improvement,
                    threshold=self.thresholds["convergence"]["min_improvement_rate"],
                    current_value=avg_improvement,
                    message=f"Low improvement rate: {avg_improvement:.3f}",
                    metadata={"improvement_rate": avg_improvement, "improvements": improvements}
                )
                self._add_signal(signal)
            
            # Convergence detection (very small changes)
            if abs(avg_improvement) < self.thresholds["convergence"]["convergence_threshold"]:
                signal = FeedbackSignal(
                    signal_type=SignalType.CONVERGENCE,
                    asset_id=asset_id,
                    value=abs(avg_improvement),
                    threshold=self.thresholds["convergence"]["convergence_threshold"],
                    current_value=abs(avg_improvement),
                    message=f"Asset may have converged: {abs(avg_improvement):.4f}",
                    metadata={"convergence_rate": abs(avg_improvement)}
                )
                self._add_signal(signal)
    
    def _check_anomalies(self, asset: ResearchAsset):
        """Check for anomalies in asset metrics"""
        asset_id = asset.id
        
        # Performance anomaly detection
        if asset_id in self.performance_history and len(self.performance_history[asset_id]) >= self.baseline_window:
            performances = [p for _, p in self.performance_history[asset_id][-self.baseline_window:]]
            
            if asset.recent_performance:
                current_performance = asset.recent_performance[-1]
                
                # Z-score anomaly detection
                mean_perf = np.mean(performances)
                std_perf = np.std(performances)
                
                if std_perf > 0:
                    z_score = abs(current_performance - mean_perf) / std_perf
                    
                    if z_score > self.alert_threshold:
                        signal = FeedbackSignal(
                            signal_type=SignalType.ANOMALY,
                            asset_id=asset_id,
                            value=z_score,
                            threshold=self.alert_threshold,
                            current_value=current_performance,
                            message=f"Performance anomaly detected (z-score: {z_score:.2f})",
                            metadata={"z_score": z_score, "baseline_mean": mean_perf, "baseline_std": std_perf}
                        )
                        self._add_signal(signal)
                        
                        self._create_alert(
                            AlertLevel.WARNING,
                            "Performance Anomaly",
                            f"Unusual performance detected for {asset.name}",
                            asset_id,
                            SignalType.ANOMALY,
                            z_score,
                            self.alert_threshold
                        )
        
        # Custom anomaly detectors
        for metric_name, detector in self.anomaly_detectors.items():
            try:
                if detector(asset):
                    signal = FeedbackSignal(
                        signal_type=SignalType.ANOMALY,
                        asset_id=asset_id,
                        value=1.0,
                        threshold=0.5,
                        current_value=1.0,
                        message=f"Custom anomaly detected: {metric_name}",
                        metadata={"detector": metric_name}
                    )
                    self._add_signal(signal)
            except Exception as e:
                logger.error(f"Error in custom anomaly detector {metric_name}: {e}")
    
    def _update_metrics(self, asset: ResearchAsset):
        """Update monitoring metrics"""
        asset_id = asset.id
        
        # Basic metrics
        metrics_to_update = [
            ("performance_current", MetricType.GAUGE, asset.expected_payoff, ""),
            ("confidence_score", MetricType.GAUGE, asset.confidence_score, ""),
            ("budget_remaining", MetricType.GAUGE, asset.current_budget, "dollars"),
            ("budget_spent", MetricType.COUNTER, asset.total_spent, "dollars"),
            ("compute_hours", MetricType.COUNTER, asset.compute_hours_used, "hours"),
            ("exploration_score", MetricType.GAUGE, asset.exploration_score, ""),
        ]
        
        for metric_name, metric_type, value, unit in metrics_to_update:
            metric = Metric(
                name=f"asset_{metric_name}",
                metric_type=metric_type,
                value=value,
                labels={"asset_id": asset_id, "asset_name": asset.name},
                unit=unit
            )
            self.metrics.append(metric)
            
            # Keep only recent metrics
            if len(self.metrics) > self.window_size * 10:
                self.metrics = self.metrics[-self.window_size * 5:]
    
    def _add_signal(self, signal: FeedbackSignal):
        """Add a feedback signal"""
        self.signals.append(signal)
        self.asset_signals[signal.asset_id].append(signal)
        
        # Call registered handlers
        if signal.signal_type in self.signal_handlers:
            for handler in self.signal_handlers[signal.signal_type]:
                try:
                    handler(signal)
                except Exception as e:
                    logger.error(f"Error in signal handler: {e}")
    
    def _create_alert(self, level: AlertLevel, title: str, message: str, 
                     asset_id: Optional[str], signal_type: SignalType, 
                     value: float, threshold: float):
        """Create an alert"""
        alert = Alert(
            alert_id=f"alert_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{len(self.alerts)}",
            level=level,
            title=title,
            message=message,
            asset_id=asset_id,
            signal_type=signal_type,
            value=value,
            threshold=threshold
        )
        self.alerts.append(alert)
        logger.warning(f"Alert created: {level.value} - {title}")
    
    def get_recent_signals(self, asset_id: Optional[str] = None, 
                         signal_type: Optional[SignalType] = None, 
                         limit: int = 50) -> List[FeedbackSignal]:
        """Get recent feedback signals"""
        signals = list(self.signals)
        
        if asset_id:
            signals = [s for s in signals if s.asset_id == asset_id]
        
        if signal_type:
            signals = [s for s in signals if s.signal_type == signal_type]
        
        # Sort by timestamp (most recent first)
        signals.sort(key=lambda s: s.timestamp, reverse=True)
        
        return signals[:limit]
    
    def get_active_alerts(self, asset_id: Optional[str] = None) -> List[Alert]:
        """Get active (unresolved) alerts"""
        alerts = [a for a in self.alerts if not a.resolved]
        
        if asset_id:
            alerts = [a for a in alerts if a.asset_id == asset_id]
        
        return alerts
    
    def acknowledge_alert(self, alert_id: str) -> bool:
        """Acknowledge an alert"""
        for alert in self.alerts:
            if alert.alert_id == alert_id:
                alert.acknowledged = True
                logger.info(f"Alert {alert_id} acknowledged")
                return True
        return False
    
    def resolve_alert(self, alert_id: str) -> bool:
        """Resolve an alert"""
        for alert in self.alerts:
            if alert.alert_id == alert_id:
                alert.resolved = True
                logger.info(f"Alert {alert_id} resolved")
                return True
        return False
    
    def get_metrics_summary(self) -> Dict:
        """Get summary of monitored metrics"""
        summary = {
            "total_signals": len(self.signals),
            "active_alerts": len(self.get_active_alerts()),
            "total_alerts": len(self.alerts),
            "metrics_tracked": len(set(m.name for m in self.metrics)),
            "assets_monitored": len(self.asset_signals),
            "signal_types": {}
        }
        
        # Count by signal type
        for signal in self.signals:
            signal_type = signal.signal_type.value
            summary["signal_types"][signal_type] = summary["signal_types"].get(signal_type, 0) + 1
        
        return summary
    
    def export_monitoring_report(self, filename: Optional[str] = None) -> str:
        """Export detailed monitoring report"""
        if filename is None:
            filename = f"monitoring_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        report = {
            "timestamp": datetime.now().isoformat(),
            "summary": self.get_metrics_summary(),
            "recent_signals": [
                {
                    "timestamp": s.timestamp.isoformat(),
                    "type": s.signal_type.value,
                    "asset_id": s.asset_id,
                    "value": s.value,
                    "threshold": s.threshold,
                    "message": s.message
                }
                for s in self.get_recent_signals(limit=20)
            ],
            "active_alerts": [
                {
                    "alert_id": a.alert_id,
                    "level": a.level.value,
                    "title": a.title,
                    "message": a.message,
                    "asset_id": a.asset_id,
                    "timestamp": a.timestamp.isoformat(),
                    "acknowledged": a.acknowledged
                }
                for a in self.get_active_alerts()
            ],
            "thresholds": self.thresholds
        }
        
        with open(filename, 'w') as f:
            json.dump(report, f, indent=2)
        
        logger.info(f"Monitoring report exported to {filename}")
        return filename

# Example usage
if __name__ == "__main__":
    from research_budget_allocator import ResearchAsset
    
    # Create monitor
    monitor = FeedbackMonitor()
    
    # Create test asset
    asset = ResearchAsset("test_asset", "Test Model", "ml", 100000, 50000)
    
    # Add performance data
    performances = [0.6, 0.65, 0.7, 0.68, 0.72, 0.75, 0.73, 0.71, 0.69, 0.45]  # Decline at end
    for perf in performances:
        asset.update_performance(perf)
        asset.spend_budget(5000, 10, 100)
        monitor.process_asset_update(asset)
    
    # Get recent signals
    recent_signals = monitor.get_recent_signals(limit=10)
    print(f"Recent signals: {len(recent_signals)}")
    for signal in recent_signals:
        print(f"  {signal.signal_type.value}: {signal.message}")
    
    # Get active alerts
    active_alerts = monitor.get_active_alerts()
    print(f"\nActive alerts: {len(active_alerts)}")
    for alert in active_alerts:
        print(f"  {alert.level.value}: {alert.title}")
    
    # Get summary
    summary = monitor.get_metrics_summary()
    print(f"\nMonitoring summary:")
    for key, value in summary.items():
        print(f"  {key}: {value}")
    
    # Export report
    report_file = monitor.export_monitoring_report()
    print(f"\nReport exported to: {report_file}")
