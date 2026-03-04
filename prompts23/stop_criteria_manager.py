"""
Stop Criteria Manager

Comprehensive system for automatically stopping unproductive research paths.
Implements multiple stopping criteria including performance thresholds, ROI analysis,
statistical significance testing, and trend analysis.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum
import logging
from datetime import datetime, timedelta
from scipy import stats
from research_budget_allocator import ResearchAsset, ResearchStatus

logger = logging.getLogger(__name__)

class StopReason(Enum):
    POOR_PERFORMANCE = "poor_performance"
    INSUFFICIENT_BUDGET = "insufficient_budget"
    LOW_ROI = "low_roi"
    STATISTICAL_INSIGNIFICANCE = "statistical_insignificance"
    TREND_DECLINE = "trend_decline"
    CONVERGENCE_FAILURE = "convergence_failure"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    EXTERNAL_STOP = "external_stop"

class StopSeverity(Enum):
    WARNING = "warning"
    CRITICAL = "critical"
    IMMEDIATE = "immediate"

@dataclass
class StopCriterion:
    """Represents a stopping criterion with its configuration"""
    name: str
    enabled: bool = True
    severity: StopSeverity = StopSeverity.WARNING
    threshold: float = 0.0
    window_size: int = 10
    min_observations: int = 5
    custom_function: Optional[Callable] = None
    
@dataclass
class StopDecision:
    """Represents a stop decision with detailed reasoning"""
    asset_id: str
    should_stop: bool
    reason: StopReason
    severity: StopSeverity
    confidence: float  # 0-1 confidence in this decision
    details: str
    metrics: Dict[str, float]
    timestamp: datetime = field(default_factory=datetime.now)

class StopCriteriaManager:
    """Manages and evaluates stopping criteria for research assets"""
    
    def __init__(self):
        self.criteria: Dict[str, StopCriterion] = {}
        self.stop_history: List[StopDecision] = []
        self.asset_stop_counts: Dict[str, int] = {}
        
        # Initialize default criteria
        self._initialize_default_criteria()
    
    def _initialize_default_criteria(self):
        """Initialize default stopping criteria"""
        self.criteria = {
            "performance_threshold": StopCriterion(
                name="Performance Threshold",
                threshold=0.1,
                window_size=5,
                min_observations=3,
                severity=StopSeverity.CRITICAL
            ),
            "consecutive_failures": StopCriterion(
                name="Consecutive Failures",
                threshold=5,  # 5 consecutive failures
                window_size=5,
                min_observations=5,
                severity=StopSeverity.IMMEDIATE
            ),
            "roi_threshold": StopCriterion(
                name="ROI Threshold",
                threshold=0.1,  # 10% ROI minimum
                window_size=10,
                min_observations=5,
                severity=StopSeverity.CRITICAL
            ),
            "budget_exhaustion": StopCriterion(
                name="Budget Exhaustion",
                threshold=0.05,  # 5% of initial budget remaining
                window_size=1,
                min_observations=1,
                severity=StopSeverity.IMMEDIATE
            ),
            "statistical_significance": StopCriterion(
                name="Statistical Insignificance",
                threshold=0.05,  # p-value threshold
                window_size=10,
                min_observations=8,
                severity=StopSeverity.WARNING
            ),
            "trend_analysis": StopCriterion(
                name="Declining Trend",
                threshold=-0.1,  # -10% trend slope
                window_size=8,
                min_observations=6,
                severity=StopSeverity.CRITICAL
            ),
            "convergence_failure": StopCriterion(
                name="Convergence Failure",
                threshold=0.01,  # Minimum improvement rate
                window_size=10,
                min_observations=8,
                severity=StopSeverity.WARNING
            ),
            "resource_exhaustion": StopCriterion(
                name="Resource Exhaustion",
                threshold=0.9,  # 90% of resources used
                window_size=1,
                min_observations=1,
                severity=StopSeverity.IMMEDIATE
            )
        }
    
    def evaluate_stop_criteria(self, asset: ResearchAsset) -> List[StopDecision]:
        """Evaluate all enabled stop criteria for an asset"""
        decisions = []
        
        for criterion_name, criterion in self.criteria.items():
            if not criterion.enabled:
                continue
            
            try:
                decision = self._evaluate_criterion(asset, criterion)
                if decision:
                    decisions.append(decision)
            except Exception as e:
                logger.error(f"Error evaluating criterion {criterion_name} for asset {asset.id}: {e}")
        
        # Sort by severity (immediate first)
        decisions.sort(key=lambda d: (d.severity.value == "immediate", 
                                   d.severity.value == "critical", 
                                   d.severity.value == "warning"), reverse=True)
        
        return decisions
    
    def _evaluate_criterion(self, asset: ResearchAsset, criterion: StopCriterion) -> Optional[StopDecision]:
        """Evaluate a single stop criterion"""
        if criterion.name == "Performance Threshold":
            return self._evaluate_performance_threshold(asset, criterion)
        elif criterion.name == "Consecutive Failures":
            return self._evaluate_consecutive_failures(asset, criterion)
        elif criterion.name == "ROI Threshold":
            return self._evaluate_roi_threshold(asset, criterion)
        elif criterion.name == "Budget Exhaustion":
            return self._evaluate_budget_exhaustion(asset, criterion)
        elif criterion.name == "Statistical Insignificance":
            return self._evaluate_statistical_significance(asset, criterion)
        elif criterion.name == "Declining Trend":
            return self._evaluate_trend_analysis(asset, criterion)
        elif criterion.name == "Convergence Failure":
            return self._evaluate_convergence_failure(asset, criterion)
        elif criterion.name == "Resource Exhaustion":
            return self._evaluate_resource_exhaustion(asset, criterion)
        elif criterion.custom_function:
            return criterion.custom_function(asset, criterion)
        
        return None
    
    def _evaluate_performance_threshold(self, asset: ResearchAsset, criterion: StopCriterion) -> Optional[StopDecision]:
        """Evaluate if performance is below threshold"""
        if len(asset.recent_performance) < criterion.min_observations:
            return None
        
        recent_performances = asset.recent_performance[-criterion.window_size:]
        avg_performance = np.mean(recent_performances)
        
        should_stop = avg_performance < criterion.threshold
        confidence = min(1.0, len(recent_performances) / criterion.window_size)
        
        if should_stop:
            return StopDecision(
                asset_id=asset.id,
                should_stop=True,
                reason=StopReason.POOR_PERFORMANCE,
                severity=criterion.severity,
                confidence=confidence,
                details=f"Average performance {avg_performance:.3f} below threshold {criterion.threshold:.3f}",
                metrics={"avg_performance": avg_performance, "threshold": criterion.threshold}
            )
        
        return None
    
    def _evaluate_consecutive_failures(self, asset: ResearchAsset, criterion: StopCriterion) -> Optional[StopDecision]:
        """Evaluate consecutive failures"""
        if len(asset.recent_performance) < criterion.min_observations:
            return None
        
        recent_performances = asset.recent_performance[-criterion.window_size:]
        failures = sum(1 for p in recent_performances if p < 0.5)  # Assume 0.5 is failure threshold
        
        should_stop = failures >= criterion.threshold
        confidence = failures / criterion.threshold if criterion.threshold > 0 else 0
        
        if should_stop:
            return StopDecision(
                asset_id=asset.id,
                should_stop=True,
                reason=StopReason.POOR_PERFORMANCE,
                severity=criterion.severity,
                confidence=confidence,
                details=f"{failures} consecutive failures out of {criterion.threshold} required",
                metrics={"failures": failures, "threshold": criterion.threshold}
            )
        
        return None
    
    def _evaluate_roi_threshold(self, asset: ResearchAsset, criterion: StopCriterion) -> Optional[StopDecision]:
        """Evaluate return on investment"""
        if asset.total_spent <= 0 or len(asset.recent_performance) < criterion.min_observations:
            return None
        
        # Calculate ROI as performance per dollar spent
        recent_performances = asset.recent_performance[-criterion.window_size:]
        avg_performance = np.mean(recent_performances)
        roi = avg_performance / (asset.total_spent / 1000)  # Performance per $1000 spent
        
        should_stop = roi < criterion.threshold
        confidence = min(1.0, len(recent_performances) / criterion.window_size)
        
        if should_stop:
            return StopDecision(
                asset_id=asset.id,
                should_stop=True,
                reason=StopReason.LOW_ROI,
                severity=criterion.severity,
                confidence=confidence,
                details=f"ROI {roi:.3f} below threshold {criterion.threshold:.3f}",
                metrics={"roi": roi, "threshold": criterion.threshold, "spent": asset.total_spent}
            )
        
        return None
    
    def _evaluate_budget_exhaustion(self, asset: ResearchAsset, criterion: StopCriterion) -> Optional[StopDecision]:
        """Evaluate if budget is exhausted"""
        budget_ratio = asset.current_budget / asset.initial_budget if asset.initial_budget > 0 else 0
        
        should_stop = budget_ratio < criterion.threshold
        confidence = 1.0  # This is deterministic
        
        if should_stop:
            return StopDecision(
                asset_id=asset.id,
                should_stop=True,
                reason=StopReason.INSUFFICIENT_BUDGET,
                severity=criterion.severity,
                confidence=confidence,
                details=f"Budget ratio {budget_ratio:.3f} below threshold {criterion.threshold:.3f}",
                metrics={"budget_ratio": budget_ratio, "current_budget": asset.current_budget}
            )
        
        return None
    
    def _evaluate_statistical_significance(self, asset: ResearchAsset, criterion: StopCriterion) -> Optional[StopDecision]:
        """Evaluate statistical significance of performance"""
        if len(asset.recent_performance) < criterion.min_observations:
            return None
        
        recent_performances = asset.recent_performance[-criterion.window_size:]
        
        # Perform one-sample t-test against null hypothesis (performance = 0.5)
        t_stat, p_value = stats.ttest_1samp(recent_performances, 0.5)
        
        should_stop = p_value > criterion.threshold  # Not significantly different from baseline
        confidence = 1 - p_value
        
        if should_stop:
            return StopDecision(
                asset_id=asset.id,
                should_stop=True,
                reason=StopReason.STATISTICAL_INSIGNIFICANCE,
                severity=criterion.severity,
                confidence=confidence,
                details=f"Performance not statistically significant (p={p_value:.3f})",
                metrics={"p_value": p_value, "t_stat": t_stat, "threshold": criterion.threshold}
            )
        
        return None
    
    def _evaluate_trend_analysis(self, asset: ResearchAsset, criterion: StopCriterion) -> Optional[StopDecision]:
        """Evaluate if there's a declining trend"""
        if len(asset.recent_performance) < criterion.min_observations:
            return None
        
        recent_performances = asset.recent_performance[-criterion.window_size:]
        x = np.arange(len(recent_performances))
        
        # Calculate linear regression slope
        slope, intercept, r_value, p_value, std_err = stats.linregress(x, recent_performances)
        
        should_stop = slope < criterion.threshold  # Negative trend
        confidence = min(1.0, abs(r_value))  # Use R-squared as confidence
        
        if should_stop:
            return StopDecision(
                asset_id=asset.id,
                should_stop=True,
                reason=StopReason.TREND_DECLINE,
                severity=criterion.severity,
                confidence=confidence,
                details=f"Declining trend with slope {slope:.3f} (threshold: {criterion.threshold:.3f})",
                metrics={"slope": slope, "r_squared": r_value**2, "p_value": p_value}
            )
        
        return None
    
    def _evaluate_convergence_failure(self, asset: ResearchAsset, criterion: StopCriterion) -> Optional[StopDecision]:
        """Evaluate if the asset is failing to converge"""
        if len(asset.recent_performance) < criterion.min_observations:
            return None
        
        recent_performances = asset.recent_performance[-criterion.window_size:]
        
        # Calculate improvement rate (change between consecutive observations)
        improvements = []
        for i in range(1, len(recent_performances)):
            improvement = (recent_performances[i] - recent_performances[i-1]) / (recent_performances[i-1] + 1e-6)
            improvements.append(improvement)
        
        if not improvements:
            return None
        
        avg_improvement = np.mean(improvements)
        
        should_stop = avg_improvement < criterion.threshold
        confidence = min(1.0, len(improvements) / (criterion.window_size - 1))
        
        if should_stop:
            return StopDecision(
                asset_id=asset.id,
                should_stop=True,
                reason=StopReason.CONVERGENCE_FAILURE,
                severity=criterion.severity,
                confidence=confidence,
                details=f"Average improvement rate {avg_improvement:.3f} below threshold {criterion.threshold:.3f}",
                metrics={"avg_improvement": avg_improvement, "improvements": improvements}
            )
        
        return None
    
    def _evaluate_resource_exhaustion(self, asset: ResearchAsset, criterion: StopCriterion) -> Optional[StopDecision]:
        """Evaluate if compute resources are exhausted"""
        # This would need to be integrated with actual resource monitoring
        # For now, use compute_hours_used as a proxy
        max_compute_hours = 10000  # Example threshold
        
        if asset.compute_hours_used >= max_compute_hours * criterion.threshold:
            return StopDecision(
                asset_id=asset.id,
                should_stop=True,
                reason=StopReason.RESOURCE_EXHAUSTION,
                severity=criterion.severity,
                confidence=1.0,
                details=f"Compute hours used {asset.compute_hours_used:.0f} exceeds threshold",
                metrics={"compute_hours": asset.compute_hours_used, "threshold": max_compute_hours * criterion.threshold}
            )
        
        return None
    
    def add_custom_criterion(self, name: str, evaluation_function: Callable[[ResearchAsset, StopCriterion], Optional[StopDecision]], 
                           threshold: float = 0.0, severity: StopSeverity = StopSeverity.WARNING):
        """Add a custom stopping criterion"""
        criterion = StopCriterion(
            name=name,
            threshold=threshold,
            severity=severity,
            custom_function=evaluation_function
        )
        self.criteria[name] = criterion
        logger.info(f"Added custom criterion: {name}")
    
    def enable_criterion(self, name: str, enabled: bool = True):
        """Enable or disable a stopping criterion"""
        if name in self.criteria:
            self.criteria[name].enabled = enabled
            logger.info(f"Criterion {name} {'enabled' if enabled else 'disabled'}")
        else:
            logger.warning(f"Criterion {name} not found")
    
    def update_criterion_threshold(self, name: str, threshold: float):
        """Update threshold for a criterion"""
        if name in self.criteria:
            self.criteria[name].threshold = threshold
            logger.info(f"Updated threshold for {name} to {threshold}")
        else:
            logger.warning(f"Criterion {name} not found")
    
    def get_stop_summary(self) -> Dict:
        """Get summary of stop decisions"""
        summary = {
            "total_decisions": len(self.stop_history),
            "assets_stopped": len(self.asset_stop_counts),
            "criteria_enabled": len([c for c in self.criteria.values() if c.enabled]),
            "recent_decisions": [
                {
                    "asset_id": d.asset_id,
                    "reason": d.reason.value,
                    "severity": d.severity.value,
                    "confidence": d.confidence,
                    "timestamp": d.timestamp.isoformat()
                }
                for d in self.stop_history[-10:]  # Last 10 decisions
            ]
        }
        
        # Count decisions by reason
        reason_counts = {}
        for decision in self.stop_history:
            reason = decision.reason.value
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        
        summary["decisions_by_reason"] = reason_counts
        
        return summary
    
    def should_stop_asset(self, asset: ResearchAsset) -> Tuple[bool, List[StopDecision]]:
        """
        Main method to determine if an asset should be stopped
        Returns: (should_stop, decisions)
        """
        decisions = self.evaluate_stop_criteria(asset)
        
        # Asset should stop if any immediate or critical decision says so
        should_stop = any(d.should_stop and d.severity in [StopSeverity.IMMEDIATE, StopSeverity.CRITICAL] 
                         for d in decisions)
        
        # Record decisions
        self.stop_history.extend(decisions)
        
        # Update stop counts
        if should_stop:
            self.asset_stop_counts[asset.id] = self.asset_stop_counts.get(asset.id, 0) + 1
        
        return should_stop, decisions

# Example usage
if __name__ == "__main__":
    from research_budget_allocator import ResearchAsset, ResearchStatus
    
    # Create stop criteria manager
    stop_manager = StopCriteriaManager()
    
    # Create test asset with poor performance
    asset = ResearchAsset("test_asset", "Test Model", "ml", 100000, 50000)
    
    # Add poor performance data
    poor_performances = [0.1, 0.05, 0.08, 0.12, 0.03, 0.07, 0.09, 0.04]
    for perf in poor_performances:
        asset.update_performance(perf)
    
    # Evaluate stop criteria
    should_stop, decisions = stop_manager.should_stop_asset(asset)
    
    print(f"Should stop asset: {should_stop}")
    print(f"Number of stop decisions: {len(decisions)}")
    
    for decision in decisions:
        if decision.should_stop:
            print(f"\nStop Decision:")
            print(f"  Reason: {decision.reason.value}")
            print(f"  Severity: {decision.severity.value}")
            print(f"  Confidence: {decision.confidence:.2f}")
            print(f"  Details: {decision.details}")
    
    # Get summary
    summary = stop_manager.get_stop_summary()
    print(f"\nSummary:")
    print(f"Total decisions: {summary['total_decisions']}")
    print(f"Assets stopped: {summary['assets_stopped']}")
    print(f"Decisions by reason: {summary['decisions_by_reason']}")
