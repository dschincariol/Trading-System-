"""
Service Level Objectives (SLOs) for Live Trading System

Defines SLOs for:
- Data freshness
- Model health  
- Execution quality
- Job reliability

All SLOs follow fail-safe principles with conservative thresholds.
"""

import time
from typing import Dict, Any, Optional
from dataclasses import dataclass
from enum import Enum

class SLOStatus(Enum):
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    VIOLATION = "violation"

@dataclass
class SLODefinition:
    name: str
    description: str
    target_percent: float  # e.g., 99.9 for 99.9%
    window_minutes: int
    alert_threshold_warning: float
    alert_threshold_critical: float
    unit: str

class TradingSLOs:
    """SLO definitions for live trading system"""
    
    # Data Freshness SLOs
    DATA_FRESHNESS = {
        "price_data": SLODefinition(
            name="price_data_freshness",
            description="Price data freshness - max age of latest price update",
            target_percent=99.5,
            window_minutes=5,
            alert_threshold_warning=120.0,  # 2 minutes
            alert_threshold_critical=300.0,  # 5 minutes
            unit="seconds"
        ),
        "market_data": SLODefinition(
            name="market_data_freshness", 
            description="Market events and news freshness",
            target_percent=99.0,
            window_minutes=15,
            alert_threshold_warning=600.0,   # 10 minutes
            alert_threshold_critical=1800.0, # 30 minutes
            unit="seconds"
        ),
        "predictions": SLODefinition(
            name="prediction_freshness",
            description="Model prediction freshness",
            target_percent=98.0,
            window_minutes=10,
            alert_threshold_warning=300.0,   # 5 minutes
            alert_threshold_critical=900.0,  # 15 minutes
            unit="seconds"
        )
    }
    
    # Model Health SLOs
    MODEL_HEALTH = {
        "prediction_accuracy": SLODefinition(
            name="prediction_accuracy",
            description="Model prediction accuracy vs actual outcomes",
            target_percent=85.0,
            window_minutes=60,
            alert_threshold_warning=75.0,
            alert_threshold_critical=65.0,
            unit="percent"
        ),
        "confidence_calibration": SLODefinition(
            name="confidence_calibration",
            description="Confidence score calibration (reliability)",
            target_percent=90.0,
            window_minutes=120,
            alert_threshold_warning=80.0,
            alert_threshold_critical=70.0,
            unit="percent"
        ),
        "model_drift": SLODefinition(
            name="model_drift",
            description="Feature distribution drift from training",
            target_percent=95.0,
            window_minutes=60,
            alert_threshold_warning=0.15,  # KL divergence
            alert_threshold_critical=0.25,
            unit="kl_divergence"
        ),
        "prediction_latency": SLODefinition(
            name="prediction_latency",
            description="Time to generate predictions",
            target_percent=99.0,
            window_minutes=5,
            alert_threshold_warning=5.0,    # seconds
            alert_threshold_critical=15.0,
            unit="seconds"
        )
    }
    
    # Execution Quality SLOs
    EXECUTION_QUALITY = {
        "fill_ratio": SLODefinition(
            name="order_fill_ratio",
            description="Percentage of orders that get filled",
            target_percent=95.0,
            window_minutes=30,
            alert_threshold_warning=85.0,
            alert_threshold_critical=75.0,
            unit="percent"
        ),
        "execution_slippage": SLODefinition(
            name="execution_slippage",
            description="Average slippage vs expected price",
            target_percent=90.0,
            window_minutes=30,
            alert_threshold_warning=5.0,    # basis points
            alert_threshold_critical=15.0,
            unit="basis_points"
        ),
        "order_latency": SLODefinition(
            name="order_latency",
            description="Time from signal to order execution",
            target_percent=95.0,
            window_minutes=15,
            alert_threshold_warning=2.0,    # seconds
            alert_threshold_critical=5.0,
            unit="seconds"
        ),
        "position_reconciliation": SLODefinition(
            name="position_reconciliation",
            description="Broker vs internal position matching",
            target_percent=99.9,
            window_minutes=10,
            alert_threshold_warning=0.1,    # percent mismatch
            alert_threshold_critical=1.0,
            unit="percent"
        )
    }
    
    # Job Reliability SLOs
    JOB_RELIABILITY = {
        "job_uptime": SLODefinition(
            name="critical_job_uptime",
            description="Uptime of critical trading jobs",
            target_percent=99.9,
            window_minutes=60,
            alert_threshold_warning=95.0,
            alert_threshold_critical=90.0,
            unit="percent"
        ),
        "job_success_rate": SLODefinition(
            name="job_success_rate",
            description="Success rate of job executions",
            target_percent=99.0,
            window_minutes=60,
            alert_threshold_warning=90.0,
            alert_threshold_critical=80.0,
            unit="percent"
        ),
        "heartbeat_freshness": SLODefinition(
            name="heartbeat_freshness",
            description="Freshness of job heartbeats",
            target_percent=99.5,
            window_minutes=10,
            alert_threshold_warning=300.0,  # 5 minutes
            alert_threshold_critical=600.0,  # 10 minutes
            unit="seconds"
        )
    }

class SLOEvaluator:
    """Evaluates current metrics against SLOs"""
    
    def __init__(self):
        self.slos = TradingSLOs()
    
    def evaluate_metric(self, slo_def: SLODefinition, current_value: float) -> SLOStatus:
        """Evaluate a single metric against its SLO"""
        
        # For metrics where lower is better (latency, drift, slippage)
        if slo_def.unit in ["seconds", "basis_points", "kl_divergence"]:
            if current_value <= slo_def.alert_threshold_warning:
                return SLOStatus.HEALTHY
            elif current_value <= slo_def.alert_threshold_critical:
                return SLOStatus.WARNING
            else:
                return SLOStatus.CRITICAL
        
        # For metrics where higher is better (accuracy, fill ratio, uptime)
        else:
            if current_value >= slo_def.target_percent:
                return SLOStatus.HEALTHY
            elif current_value >= slo_def.alert_threshold_critical:
                return SLOStatus.WARNING
            else:
                return SLOStatus.CRITICAL
    
    def get_all_slos(self) -> Dict[str, Dict[str, SLODefinition]]:
        """Get all SLO definitions organized by category"""
        return {
            "data_freshness": self.slos.DATA_FRESHNESS,
            "model_health": self.slos.MODEL_HEALTH,
            "execution_quality": self.slos.EXECUTION_QUALITY,
            "job_reliability": self.slos.JOB_RELIABILITY
        }
    
    def get_slo_by_name(self, category: str, name: str) -> Optional[SLODefinition]:
        """Get specific SLO by category and name"""
        all_slos = self.get_all_slos()
        if category in all_slos and name in all_slos[category]:
            return all_slos[category][name]
        return None

# Global SLO evaluator instance
slo_evaluator = SLOEvaluator()
