# model_fragility_analyzer.py
"""
Model Fragility Analyzer - Advanced failure detection and fragility measurement.

This module provides sophisticated analysis of model failure modes and fragility patterns
under stress conditions. It identifies specific weaknesses and provides actionable insights
for model improvement.

Key Features:
- Multi-dimensional fragility scoring
- Failure mode classification and clustering
- Temporal fragility patterns
- Cross-regime fragility analysis
- Early warning indicators
- Remediation recommendations
"""

import os
import json
import math
import logging
import numpy as np
from dataclasses import dataclass, asdict
from typing import Dict, List, Any, Optional, Tuple, Set
from datetime import datetime, timedelta
from enum import Enum
from collections import defaultdict, Counter

from engine.storage import connect
from engine.research.adversarial_scenario_generator import ScenarioType, ScenarioResult


class FailureMode(Enum):
    """Classification of model failure modes"""
    RETURN_DEGRADATION = "return_degradation"
    EXCESSIVE_DRAWDOWN = "excessive_drawdown"
    VOLATILITY_EXPLOSION = "volatility_explosion"
    EXECUTION_COST_SPIKE = "execution_cost_spike"
    PREDICTION_ACCURACY_LOSS = "prediction_accuracy_loss"
    CORRELATION_BREAKDOWN = "correlation_breakdown"
    LIQUIDITY_CRISIS = "liquidity_crisis"
    REGIME_MISALIGNMENT = "regime_misalignment"
    TIMING_FAILURE = "timing_failure"
    POSITION_SIZING_FAILURE = "position_sizing_failure"


class FragilityDimension(Enum):
    """Dimensions of model fragility"""
    PERFORMANCE = "performance"  # Return/sharpe degradation
    RISK = "risk"  # Drawdown/volatility issues
    EXECUTION = "execution"  # Cost/slippage problems
    PREDICTION = "prediction"  # Accuracy/reliability
    STABILITY = "stability"  # Consistency across regimes


@dataclass
class FragilityProfile:
    """Comprehensive fragility profile for a model"""
    model_name: str
    model_kind: str
    model_ts_ms: int
    overall_fragility: float
    dimension_scores: Dict[FragilityDimension, float]
    failure_modes: List[FailureMode]
    failure_mode_frequencies: Dict[FailureMode, float]
    scenario_vulnerabilities: Dict[ScenarioType, float]
    temporal_patterns: Dict[str, Any]
    early_warning_indicators: List[str]
    remediation_priorities: List[Dict[str, Any]]
    last_updated_ts_ms: int
    
    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result['dimension_scores'] = {k.value: v for k, v in self.dimension_scores.items()}
        result['failure_modes'] = [m.value for m in self.failure_modes]
        result['failure_mode_frequencies'] = {k.value: v for k, v in self.failure_mode_frequencies.items()}
        result['scenario_vulnerabilities'] = {k.value: v for k, v in self.scenario_vulnerabilities.items()}
        return result


@dataclass
class FailureCluster:
    """Cluster of similar failure patterns"""
    cluster_id: str
    failure_modes: List[FailureMode]
    scenario_types: List[ScenarioType]
    avg_fragility: float
    scenario_count: int
    common_triggers: List[str]
    recommended_fixes: List[str]


class ModelFragilityAnalyzer:
    """Advanced analyzer for model fragility and failure patterns"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self._ensure_tables()
        self.failure_mode_weights = self._initialize_failure_weights()
        self.dimension_weights = self._initialize_dimension_weights()
    
    def _ensure_tables(self):
        """Create database tables for fragility analysis"""
        schema = """
        CREATE TABLE IF NOT EXISTS model_fragility_profiles (
            model_name TEXT NOT NULL,
            model_kind TEXT NOT NULL,
            model_ts_ms INTEGER NOT NULL,
            profile_json TEXT NOT NULL,
            created_ts_ms INTEGER NOT NULL,
            PRIMARY KEY (model_name, model_kind, model_ts_ms)
        );
        
        CREATE TABLE IF NOT EXISTS failure_clusters (
            cluster_id TEXT PRIMARY KEY,
            cluster_json TEXT NOT NULL,
            created_ts_ms INTEGER NOT NULL,
            updated_ts_ms INTEGER NOT NULL
        );
        
        CREATE TABLE IF NOT EXISTS fragility_trends (
            model_name TEXT NOT NULL,
            fragility_dimension TEXT NOT NULL,
            ts_ms INTEGER NOT NULL,
            fragility_score REAL NOT NULL,
            scenario_count INTEGER NOT NULL,
            PRIMARY KEY (model_name, fragility_dimension, ts_ms)
        );
        
        CREATE TABLE IF NOT EXISTS early_warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_name TEXT NOT NULL,
            warning_type TEXT NOT NULL,
            severity TEXT NOT NULL,  -- 'low', 'medium', 'high', 'critical'
            message TEXT NOT NULL,
            metrics_json TEXT,
            created_ts_ms INTEGER NOT NULL,
            acknowledged BOOLEAN DEFAULT 0,
            acknowledged_ts_ms INTEGER
        );
        
        CREATE INDEX IF NOT EXISTS idx_fragility_profiles_model ON model_fragility_profiles(model_name);
        CREATE INDEX IF NOT EXISTS idx_fragility_trends_model ON fragility_trends(model_name, fragility_dimension);
        CREATE INDEX IF NOT EXISTS idx_warnings_model ON early_warnings(model_name, created_ts_ms);
        """
        
        con = connect()
        try:
            con.executescript(schema)
            con.commit()
        finally:
            con.close()
    
    def _initialize_failure_weights(self) -> Dict[FailureMode, float]:
        """Initialize weights for different failure modes"""
        return {
            FailureMode.RETURN_DEGRADATION: 0.2,
            FailureMode.EXCESSIVE_DRAWDOWN: 0.25,
            FailureMode.VOLATILITY_EXPLOSION: 0.15,
            FailureMode.EXECUTION_COST_SPIKE: 0.1,
            FailureMode.PREDICTION_ACCURACY_LOSS: 0.15,
            FailureMode.CORRELATION_BREAKDOWN: 0.05,
            FailureMode.LIQUIDITY_CRISIS: 0.05,
            FailureMode.REGIME_MISALIGNMENT: 0.03,
            FailureMode.TIMING_FAILURE: 0.01,
            FailureMode.POSITION_SIZING_FAILURE: 0.01,
        }
    
    def _initialize_dimension_weights(self) -> Dict[FragilityDimension, float]:
        """Initialize weights for fragility dimensions"""
        return {
            FragilityDimension.PERFORMANCE: 0.3,
            FragilityDimension.RISK: 0.3,
            FragilityDimension.EXECUTION: 0.2,
            FragilityDimension.PREDICTION: 0.15,
            FragilityDimension.STABILITY: 0.05,
        }
    
    def analyze_model_fragility(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        stress_test_results: Optional[List[ScenarioResult]] = None
    ) -> FragilityProfile:
        """Comprehensive fragility analysis for a specific model"""
        
        self.logger.info(f"Analyzing fragility for {model_name}:{model_kind}@{model_ts_ms}")
        
        # Get stress test results if not provided
        if stress_test_results is None:
            stress_test_results = self._get_stress_test_results(model_name, model_kind, model_ts_ms)
        
        if not stress_test_results:
            self.logger.warning(f"No stress test results found for {model_name}")
            return self._create_empty_profile(model_name, model_kind, model_ts_ms)
        
        # Analyze each dimension
        dimension_scores = self._analyze_fragility_dimensions(stress_test_results)
        
        # Identify failure modes and patterns
        failure_modes, failure_frequencies = self._analyze_failure_modes(stress_test_results)
        
        # Analyze scenario vulnerabilities
        scenario_vulnerabilities = self._analyze_scenario_vulnerabilities(stress_test_results)
        
        # Detect temporal patterns
        temporal_patterns = self._analyze_temporal_patterns(stress_test_results)
        
        # Identify early warning indicators
        early_warnings = self._identify_early_warnings(stress_test_results)
        
        # Generate remediation priorities
        remediation = self._generate_remediation_priorities(
            dimension_scores, failure_modes, scenario_vulnerabilities
        )
        
        # Calculate overall fragility
        overall_fragility = self._calculate_overall_fragility(dimension_scores)
        
        profile = FragilityProfile(
            model_name=model_name,
            model_kind=model_kind,
            model_ts_ms=model_ts_ms,
            overall_fragility=overall_fragility,
            dimension_scores=dimension_scores,
            failure_modes=failure_modes,
            failure_mode_frequencies=failure_frequencies,
            scenario_vulnerabilities=scenario_vulnerabilities,
            temporal_patterns=temporal_patterns,
            early_warning_indicators=early_warnings,
            remediation_priorities=remediation,
            last_updated_ts_ms=int(datetime.now().timestamp() * 1000)
        )
        
        # Store profile
        self._store_fragility_profile(profile)
        
        # Store trends
        self._store_fragility_trends(profile)
        
        # Generate early warnings if needed
        self._generate_early_warnings(profile)
        
        return profile
    
    def _get_stress_test_results(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int
    ) -> List[ScenarioResult]:
        """Retrieve stress test results for a model"""
        con = connect()
        try:
            # Get stress test gates for this model
            rows = con.execute("""
                SELECT test_batch_id FROM stress_test_gates
                WHERE model_name = ? AND model_kind = ? AND model_ts_ms = ?
                ORDER BY created_ts_ms DESC
            """, (model_name, model_kind, model_ts_ms)).fetchall()
            
            results = []
            for row in rows:
                batch_id = row[0]
                
                # Get stress test summary
                summary_row = con.execute("""
                    SELECT summary_json FROM stress_test_summary
                    WHERE test_batch_id = ?
                """, (batch_id,)).fetchone()
                
                if summary_row:
                    summary = json.loads(summary_row[0])
                    # Convert to ScenarioResult objects (simplified)
                    for result_data in summary.get('results', []):
                        # This is a simplified conversion - in practice, you'd reconstruct full objects
                        pass
            return results
        finally:
            con.close()
    
    def _analyze_fragility_dimensions(
        self,
        stress_results: List[ScenarioResult]
    ) -> Dict[FragilityDimension, float]:
        """Analyze fragility across different dimensions"""
        
        dimension_scores = {dim: 0.0 for dim in FragilityDimension}
        scenario_count = len(stress_results)
        
        if scenario_count == 0:
            return dimension_scores
        
        for result in stress_results:
            fragility = result.fragility_score
            
            # Performance dimension (return, sharpe degradation)
            perf_score = self._calculate_performance_fragility(result)
            dimension_scores[FragilityDimension.PERFORMANCE] += perf_score
            
            # Risk dimension (drawdown, volatility)
            risk_score = self._calculate_risk_fragility(result)
            dimension_scores[FragilityDimension.RISK] += risk_score
            
            # Execution dimension (costs, slippage)
            exec_score = self._calculate_execution_fragility(result)
            dimension_scores[FragilityDimension.EXECUTION] += exec_score
            
            # Prediction dimension (accuracy errors)
            pred_score = self._calculate_prediction_fragility(result)
            dimension_scores[FragilityDimension.PREDICTION] += pred_score
            
            # Stability dimension (consistency)
            stability_score = self._calculate_stability_fragility(result)
            dimension_scores[FragilityDimension.STABILITY] += stability_score
        
        # Average across scenarios
        for dim in dimension_scores:
            dimension_scores[dim] /= scenario_count
        
        return dimension_scores
    
    def _calculate_performance_fragility(self, result: ScenarioResult) -> float:
        """Calculate performance fragility score"""
        base_metrics = result.baseline_metrics
        stress_metrics = result.stress_metrics
        
        # Return degradation
        base_ret = base_metrics.get('total_return', 0.0)
        stress_ret = stress_metrics.get('total_return', 0.0)
        return_loss = max(0, (base_ret - stress_ret) / max(abs(base_ret), 0.01))
        
        # Sharpe degradation
        base_sharpe = base_metrics.get('sharpe_simple', 0.0)
        stress_sharpe = stress_metrics.get('sharpe_simple', 0.0)
        sharpe_loss = max(0, (base_sharpe - stress_sharpe) / max(abs(base_sharpe), 0.01))
        
        return min(1.0, 0.6 * return_loss + 0.4 * sharpe_loss)
    
    def _calculate_risk_fragility(self, result: ScenarioResult) -> float:
        """Calculate risk fragility score"""
        base_metrics = result.baseline_metrics
        stress_metrics = result.stress_metrics
        
        # Drawdown increase
        base_dd = abs(base_metrics.get('max_drawdown', 0.0))
        stress_dd = abs(stress_metrics.get('max_drawdown', 0.0))
        dd_increase = max(0, (stress_dd - base_dd) / max(base_dd, 0.01))
        
        # Volatility increase
        base_vol = base_metrics.get('ret_volatility', 0.0)
        stress_vol = stress_metrics.get('ret_volatility', 0.0)
        vol_increase = max(0, (stress_vol - base_vol) / max(base_vol, 0.01))
        
        return min(1.0, 0.7 * dd_increase + 0.3 * vol_increase)
    
    def _calculate_execution_fragility(self, result: ScenarioResult) -> float:
        """Calculate execution fragility score"""
        base_metrics = result.baseline_metrics
        stress_metrics = result.stress_metrics
        
        # Execution cost increase
        base_cost = base_metrics.get('total_exec_cost', 0.0)
        stress_cost = stress_metrics.get('total_exec_cost', 0.0)
        cost_increase = max(0, (stress_cost - base_cost) / max(base_cost, 0.001))
        
        # Slippage increase
        base_slip = base_metrics.get('total_slippage', 0.0)
        stress_slip = stress_metrics.get('total_slippage', 0.0)
        slip_increase = max(0, (stress_slip - base_slip) / max(base_slip, 0.001))
        
        return min(1.0, 0.6 * cost_increase + 0.4 * slip_increase)
    
    def _calculate_prediction_fragility(self, result: ScenarioResult) -> float:
        """Calculate prediction fragility score"""
        base_metrics = result.baseline_metrics
        stress_metrics = result.stress_metrics
        
        # RMSE increase
        base_rmse = base_metrics.get('rmse_net', base_metrics.get('rmse', 0.0))
        stress_rmse = stress_metrics.get('rmse_net', stress_metrics.get('rmse', 0.0))
        rmse_increase = max(0, (stress_rmse - base_rmse) / max(base_rmse, 0.001))
        
        # Directional accuracy decrease
        base_dir = base_metrics.get('directional_acc_net', base_metrics.get('directional_acc', 0.0))
        stress_dir = stress_metrics.get('directional_acc_net', stress_metrics.get('directional_acc', 0.0))
        dir_loss = max(0, (base_dir - stress_dir) / max(base_dir, 0.01))
        
        return min(1.0, 0.7 * rmse_increase + 0.3 * dir_loss)
    
    def _calculate_stability_fragility(self, result: ScenarioResult) -> float:
        """Calculate stability fragility score"""
        # Stability is about consistency across different scenarios
        # For now, use the variance in performance across similar scenarios
        
        # This is a simplified implementation
        # In practice, you'd analyze multiple scenarios of the same type
        return result.fragility_score * 0.5  # Placeholder
    
    def _analyze_failure_modes(
        self,
        stress_results: List[ScenarioResult]
    ) -> Tuple[List[FailureMode], Dict[FailureMode, float]]:
        """Analyze and classify failure modes"""
        
        mode_counts = Counter()
        total_scenarios = len(stress_results)
        
        for result in stress_results:
            for mode_str in result.failure_modes:
                try:
                    mode = FailureMode(mode_str)
                    mode_counts[mode] += 1
                except ValueError:
                    # Unknown failure mode
                    continue
        
        # Convert to frequencies
        mode_frequencies = {mode: count / total_scenarios for mode, count in mode_counts.items()}
        
        # Get most common failure modes (top 5)
        common_modes = [mode for mode, _ in mode_counts.most_common(5)]
        
        return common_modes, mode_frequencies
    
    def _analyze_scenario_vulnerabilities(
        self,
        stress_results: List[ScenarioResult]
    ) -> Dict[ScenarioType, float]:
        """Analyze vulnerability to different scenario types"""
        
        scenario_vulns = defaultdict(list)
        
        for result in stress_results:
            scenario_type = result.parameters.scenario_type
            scenario_vulns[scenario_type].append(result.fragility_score)
        
        # Calculate average fragility per scenario type
        vulnerabilities = {}
        for scenario_type, scores in scenario_vulns.items():
            vulnerabilities[scenario_type] = sum(scores) / len(scores)
        
        return vulnerabilities
    
    def _analyze_temporal_patterns(
        self,
        stress_results: List[ScenarioResult]
    ) -> Dict[str, Any]:
        """Analyze temporal patterns in fragility"""
        
        patterns = {
            'duration_correlation': self._analyze_duration_correlation(stress_results),
            'severity_correlation': self._analyze_severity_correlation(stress_results),
            'recovery_patterns': self._analyze_recovery_patterns(stress_results),
            'time_to_failure': self._analyze_time_to_failure(stress_results)
        }
        
        return patterns
    
    def _analyze_duration_correlation(self, stress_results: List[ScenarioResult]) -> float:
        """Analyze correlation between scenario duration and fragility"""
        durations = [result.parameters.duration_minutes for result in stress_results]
        fragilities = [result.fragility_score for result in stress_results]
        
        if len(durations) < 2:
            return 0.0
        
        correlation = np.corrcoef(durations, fragilities)[0, 1]
        return float(correlation) if not math.isnan(correlation) else 0.0
    
    def _analyze_severity_correlation(self, stress_results: List[ScenarioResult]) -> float:
        """Analyze correlation between scenario severity and fragility"""
        severities = [result.parameters.severity for result in stress_results]
        fragilities = [result.fragility_score for result in stress_results]
        
        if len(severities) < 2:
            return 0.0
        
        correlation = np.corrcoef(severities, fragilities)[0, 1]
        return float(correlation) if not math.isnan(correlation) else 0.0
    
    def _analyze_recovery_patterns(self, stress_results: List[ScenarioResult]) -> Dict[str, float]:
        """Analyze recovery time patterns"""
        recovery_times = [result.recovery_time_ms for result in stress_results]
        
        if not recovery_times:
            return {}
        
        return {
            'avg_recovery_time_ms': sum(recovery_times) / len(recovery_times),
            'max_recovery_time_ms': max(recovery_times),
            'min_recovery_time_ms': min(recovery_times)
        }
    
    def _analyze_time_to_failure(self, stress_results: List[ScenarioResult]) -> Dict[str, float]:
        """Analyze time to failure patterns"""
        # This would require more granular data from the backtest
        # For now, return placeholder
        return {
            'avg_time_to_failure_min': 15.0,  # Placeholder
            'failure_acceleration': 0.1  # Placeholder
        }
    
    def _identify_early_warnings(self, stress_results: List[ScenarioResult]) -> List[str]:
        """Identify early warning indicators"""
        warnings = []
        
        # High fragility in specific scenarios
        scenario_vulns = self._analyze_scenario_vulnerabilities(stress_results)
        for scenario_type, avg_fragility in scenario_vulns.items():
            if avg_fragility > 0.8:
                warnings.append(f"High vulnerability to {scenario_type.value} scenarios")
        
        # Consistent failure modes
        mode_freqs = self._analyze_failure_modes(stress_results)[1]
        for mode, frequency in mode_freqs.items():
            if frequency > 0.6:  # Appears in >60% of scenarios
                warnings.append(f"Frequent {mode.value} failures")
        
        # Poor recovery
        recovery_patterns = self._analyze_recovery_patterns(stress_results)
        avg_recovery = recovery_patterns.get('avg_recovery_time_ms', 0)
        if avg_recovery > 4 * 60 * 60 * 1000:  # > 4 hours
            warnings.append("Slow recovery from stress events")
        
        # High execution cost sensitivity
        exec_fragility = max(
            self._calculate_execution_fragility(result) for result in stress_results
        )
        if exec_fragility > 0.7:
            warnings.append("High sensitivity to execution cost increases")
        
        return warnings
    
    def _generate_remediation_priorities(
        self,
        dimension_scores: Dict[FragilityDimension, float],
        failure_modes: List[FailureMode],
        scenario_vulnerabilities: Dict[ScenarioType, float]
    ) -> List[Dict[str, Any]]:
        """Generate prioritized remediation recommendations"""
        
        priorities = []
        
        # Prioritize by dimension scores
        sorted_dimensions = sorted(
            dimension_scores.items(),
            key=lambda x: x[1],
            reverse=True
        )
        
        for dimension, score in sorted_dimensions[:3]:  # Top 3 dimensions
            if score > 0.6:  # Only include high fragility dimensions
                priority = {
                    'dimension': dimension.value,
                    'fragility_score': score,
                    'recommended_actions': self._get_remediation_actions(dimension, failure_modes),
                    'priority': 'high' if score > 0.8 else 'medium',
                    'estimated_effort': self._estimate_effort(dimension),
                    'expected_impact': self._estimate_impact(dimension)
                }
                priorities.append(priority)
        
        # Add scenario-specific recommendations
        for scenario_type, fragility in scenario_vulnerabilities.items():
            if fragility > 0.7:
                priority = {
                    'scenario_type': scenario_type.value,
                    'fragility_score': fragility,
                    'recommended_actions': self._get_scenario_remediation(scenario_type),
                    'priority': 'high' if fragility > 0.85 else 'medium',
                    'estimated_effort': 'medium',
                    'expected_impact': 'high'
                }
                priorities.append(priority)
        
        # Sort by combined priority (fragility score * priority weight)
        priority_weights = {'high': 3.0, 'medium': 2.0, 'low': 1.0}
        priorities.sort(
            key=lambda x: x['fragility_score'] * priority_weights.get(x['priority'], 1.0),
            reverse=True
        )
        
        return priorities[:5]  # Return top 5 priorities
    
    def _get_remediation_actions(self, dimension: FragilityDimension, failure_modes: List[FailureMode]) -> List[str]:
        """Get specific remediation actions for a fragility dimension"""
        action_map = {
            FragilityDimension.PERFORMANCE: [
                "Review and optimize feature engineering",
                "Implement ensemble methods for stability",
                "Add regime-aware model components",
                "Optimize position sizing algorithms"
            ],
            FragilityDimension.RISK: [
                "Implement dynamic drawdown controls",
                "Add volatility targeting mechanisms",
                "Enhanced risk management overlays",
                "Portfolio diversification improvements"
            ],
            FragilityDimension.EXECUTION: [
                "Optimize execution algorithms",
                "Implement adaptive spread monitoring",
                "Add liquidity-aware order sizing",
                "Improve market impact modeling"
            ],
            FragilityDimension.PREDICTION: [
                "Retrain with stress-augmented data",
                "Add uncertainty quantification",
                "Implement confidence-based filtering",
                "Enhance feature robustness"
            ],
            FragilityDimension.STABILITY: [
                "Add cross-validation across regimes",
                "Implement stability testing",
                "Regular model recalibration",
                "Add ensemble diversity"
            ]
        }
        
        return action_map.get(dimension, ["General model improvements"])
    
    def _get_scenario_remediation(self, scenario_type: ScenarioType) -> List[str]:
        """Get scenario-specific remediation actions"""
        remediation_map = {
            ScenarioType.MARKET_CRASH: [
                "Add crash detection indicators",
                "Implement defensive position sizing",
                "Add volatility-based position limits"
            ],
            ScenarioType.LIQUIDITY_DROUGHT: [
                "Add liquidity monitoring",
                "Implement liquidity-aware execution",
                "Add market depth analysis"
            ],
            ScenarioType.NEWS_SHOCK: [
                "Add sentiment analysis features",
                "Implement news-driven position adjustments",
                "Add event-driven risk controls"
            ],
            ScenarioType.REGIME_SHIFT: [
                "Add regime detection models",
                "Implement regime-specific parameters",
                "Add smooth regime transitions"
            ]
        }
        
        return remediation_map.get(scenario_type, ["General robustness improvements"])
    
    def _estimate_effort(self, dimension: FragilityDimension) -> str:
        """Estimate implementation effort for dimension fixes"""
        effort_map = {
            FragilityDimension.PERFORMANCE: 'high',
            FragilityDimension.RISK: 'medium',
            FragilityDimension.EXECUTION: 'medium',
            FragilityDimension.PREDICTION: 'high',
            FragilityDimension.STABILITY: 'medium'
        }
        return effort_map.get(dimension, 'medium')
    
    def _estimate_impact(self, dimension: FragilityDimension) -> str:
        """Estimate impact of dimension fixes"""
        impact_map = {
            FragilityDimension.PERFORMANCE: 'high',
            FragilityDimension.RISK: 'high',
            FragilityDimension.EXECUTION: 'medium',
            FragilityDimension.PREDICTION: 'high',
            FragilityDimension.STABILITY: 'medium'
        }
        return impact_map.get(dimension, 'medium')
    
    def _calculate_overall_fragility(self, dimension_scores: Dict[FragilityDimension, float]) -> float:
        """Calculate overall fragility score from dimension scores"""
        weighted_score = 0.0
        total_weight = 0.0
        
        for dimension, score in dimension_scores.items():
            weight = self.dimension_weights.get(dimension, 0.2)
            weighted_score += score * weight
            total_weight += weight
        
        return weighted_score / total_weight if total_weight > 0 else 0.0
    
    def _create_empty_profile(self, model_name: str, model_kind: str, model_ts_ms: int) -> FragilityProfile:
        """Create an empty fragility profile when no data is available"""
        return FragilityProfile(
            model_name=model_name,
            model_kind=model_kind,
            model_ts_ms=model_ts_ms,
            overall_fragility=0.0,
            dimension_scores={dim: 0.0 for dim in FragilityDimension},
            failure_modes=[],
            failure_mode_frequencies={},
            scenario_vulnerabilities={},
            temporal_patterns={},
            early_warning_indicators=[],
            remediation_priorities=[],
            last_updated_ts_ms=int(datetime.now().timestamp() * 1000)
        )
    
    def _store_fragility_profile(self, profile: FragilityProfile):
        """Store fragility profile in database"""
        con = connect()
        try:
            con.execute("""
                INSERT OR REPLACE INTO model_fragility_profiles
                (model_name, model_kind, model_ts_ms, profile_json, created_ts_ms)
                VALUES (?, ?, ?, ?, ?)
            """, (profile.model_name, profile.model_kind, profile.model_ts_ms,
                  json.dumps(profile.to_dict()), profile.last_updated_ts_ms))
            con.commit()
        finally:
            con.close()
    
    def _store_fragility_trends(self, profile: FragilityProfile):
        """Store fragility trend data"""
        con = connect()
        try:
            for dimension, score in profile.dimension_scores.items():
                con.execute("""
                    INSERT OR REPLACE INTO fragility_trends
                    (model_name, fragility_dimension, ts_ms, fragility_score, scenario_count)
                    VALUES (?, ?, ?, ?, ?)
                """, (profile.model_name, dimension.value, profile.last_updated_ts_ms,
                      score, 1))  # scenario_count would be actual count
            con.commit()
        finally:
            con.close()
    
    def _generate_early_warnings(self, profile: FragilityProfile):
        """Generate early warning alerts based on fragility profile"""
        con = connect()
        try:
            # Check for critical fragility
            if profile.overall_fragility > 0.85:
                con.execute("""
                    INSERT INTO early_warnings
                    (model_name, warning_type, severity, message, metrics_json, created_ts_ms)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (profile.model_name, 'high_fragility', 'critical',
                      f"Model shows critical fragility score: {profile.overall_fragility:.3f}",
                      json.dumps({'overall_fragility': profile.overall_fragility}),
                      profile.last_updated_ts_ms))
            
            # Check for specific dimension issues
            for dimension, score in profile.dimension_scores.items():
                if score > 0.8:
                    con.execute("""
                        INSERT INTO early_warnings
                        (model_name, warning_type, severity, message, metrics_json, created_ts_ms)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (profile.model_name, f'{dimension.value}_fragility', 'high',
                          f"High fragility in {dimension.value}: {score:.3f}",
                          json.dumps({f'{dimension.value}_fragility': score}),
                          profile.last_updated_ts_ms))
            
            con.commit()
        finally:
            con.close()
    
    def cluster_failure_patterns(self, model_name: Optional[str] = None) -> List[FailureCluster]:
        """Cluster similar failure patterns across models"""
        # This is a simplified implementation
        # In practice, you'd use clustering algorithms like DBSCAN or hierarchical clustering
        
        con = connect()
        try:
            # Get all recent fragility profiles
            query = """
                SELECT profile_json FROM model_fragility_profiles
                WHERE created_ts_ms > ?
            """
            params = [(int((datetime.now() - timedelta(days=30)).timestamp() * 1000)),)]
            
            if model_name:
                query += " AND model_name = ?"
                params.append(model_name)
            
            rows = con.execute(query, params).fetchall()
            
            profiles = []
            for row in rows:
                profile_data = json.loads(row[0])
                profiles.append(profile_data)
            
            # Simple clustering based on common failure modes
            clusters = self._simple_failure_mode_clustering(profiles)
            
            return clusters
        finally:
            con.close()
    
    def _simple_failure_mode_clustering(self, profiles: List[Dict[str, Any]]) -> List[FailureCluster]:
        """Simple clustering based on common failure modes"""
        
        # Group by common failure mode patterns
        mode_groups = defaultdict(list)
        
        for profile in profiles:
            failure_modes = profile.get('failure_modes', [])
            if failure_modes:
                # Use sorted tuple of failure modes as key
                mode_key = tuple(sorted(failure_modes[:3]))  # Top 3 modes
                mode_groups[mode_key].append(profile)
        
        clusters = []
        for i, (mode_key, group_profiles) in enumerate(mode_groups.items()):
            if len(group_profiles) >= 2:  # Only create clusters with 2+ members
                avg_fragility = sum(p.get('overall_fragility', 0) for p in group_profiles) / len(group_profiles)
                
                cluster = FailureCluster(
                    cluster_id=f"cluster_{i}",
                    failure_modes=[FailureMode(mode) for mode in mode_key],
                    scenario_types=[],  # Would need more detailed analysis
                    avg_fragility=avg_fragility,
                    scenario_count=len(group_profiles),
                    common_triggers=self._identify_common_triggers(group_profiles),
                    recommended_fixes=self._get_cluster_remediation(mode_key)
                )
                clusters.append(cluster)
        
        return clusters
    
    def _identify_common_triggers(self, profiles: List[Dict[str, Any]]) -> List[str]:
        """Identify common triggers for failure patterns"""
        triggers = []
        
        # Analyze scenario vulnerabilities
        scenario_counts = defaultdict(int)
        for profile in profiles:
            scenario_vulns = profile.get('scenario_vulnerabilities', {})
            for scenario, fragility in scenario_vulns.items():
                if fragility > 0.7:
                    scenario_counts[scenario] += 1
        
        # Get most common triggers
        common_scenarios = sorted(scenario_counts.items(), key=lambda x: x[1], reverse=True)[:3]
        triggers.extend([f"{scenario} scenarios" for scenario, _ in common_scenarios])
        
        return triggers
    
    def _get_cluster_remediation(self, failure_modes: Tuple[str, ...]) -> List[str]:
        """Get remediation recommendations for failure mode clusters"""
        remediation_map = {
            ('return_degradation', 'excessive_drawdown'): [
                "Implement stronger risk controls",
                "Add dynamic position sizing",
                "Enhanced drawdown monitoring"
            ],
            ('volatility_explosion', 'execution_cost_spike'): [
                "Add volatility-based position limits",
                "Optimize execution algorithms",
                "Implement spread monitoring"
            ],
            ('prediction_accuracy_loss',): [
                "Retrain with augmented data",
                "Add uncertainty quantification",
                "Feature engineering improvements"
            ]
        }
        
        # Find matching remediation
        for modes, actions in remediation_map.items():
            if any(mode in failure_modes for mode in modes):
                return actions
        
        return ["General model robustness improvements"]
    
    def get_fragility_report(self, model_name: str, model_kind: Optional[str] = None) -> str:
        """Generate comprehensive fragility analysis report"""
        
        con = connect()
        try:
            # Get latest profile
            query = """
                SELECT profile_json FROM model_fragility_profiles
                WHERE model_name = ?
            """
            params = [model_name]
            
            if model_kind:
                query += " AND model_kind = ?"
                params.append(model_kind)
            
            query += " ORDER BY created_ts_ms DESC LIMIT 1"
            
            row = con.execute(query, params).fetchone()
            
            if not row:
                return f"No fragility profile found for {model_name}"
            
            profile_data = json.loads(row[0])
        finally:
            con.close()
        
        # Generate report
        report = f"""
# Model Fragility Analysis Report

**Model:** {profile_data['model_name']} ({profile_data['model_kind']})
**Analysis Date:** {datetime.fromtimestamp(profile_data['last_updated_ts_ms']/1000).strftime('%Y-%m-%d %H:%M:%S')}

## Executive Summary
- **Overall Fragility Score:** {profile_data['overall_fragility']:.3f}
- **Risk Level:** {'Critical' if profile_data['overall_fragility'] > 0.8 else 'High' if profile_data['overall_fragility'] > 0.6 else 'Medium' if profile_data['overall_fragility'] > 0.4 else 'Low'}
- **Primary Failure Modes:** {', '.join(profile_data['failure_modes'][:3])}

## Dimensional Analysis
| Dimension | Fragility Score | Status |
|-----------|-----------------|---------|
"""
        
        for dimension, score in profile_data['dimension_scores'].items():
            status = 'Critical' if score > 0.8 else 'High' if score > 0.6 else 'Medium' if score > 0.4 else 'Low'
            report += f"| {dimension.replace('_', ' ').title()} | {score:.3f} | {status} |\n"
        
        report += f"""
## Scenario Vulnerabilities
"""
        
        for scenario, fragility in profile_data['scenario_vulnerabilities'].items():
            risk_level = 'Critical' if fragility > 0.8 else 'High' if fragility > 0.6 else 'Medium'
            report += f"- **{scenario.replace('_', ' ').title()}:** {fragility:.3f} ({risk_level})\n"
        
        report += f"""
## Early Warning Indicators
"""
        
        for warning in profile_data['early_warning_indicators']:
            report += f"⚠️ {warning}\n"
        
        report += f"""
## Remediation Priorities
"""
        
        for i, priority in enumerate(profile_data['remediation_priorities'], 1):
            report += f"""
### {i}. {priority.get('dimension', priority.get('scenario_type', 'Unknown')).replace('_', ' ').title()} 
**Priority:** {priority['priority'].upper()} | **Fragility:** {priority['fragility_score']:.3f}
**Effort:** {priority['estimated_effort']} | **Expected Impact:** {priority['expected_impact']}

**Recommended Actions:**
"""
            for action in priority['recommended_actions']:
                report += f"- {action}\n"
        
        report += f"""
## Temporal Patterns
- **Duration-Fragility Correlation:** {profile_data['temporal_patterns']['duration_correlation']:.3f}
- **Severity-Fragility Correlation:** {profile_data['temporal_patterns']['severity_correlation']:.3f}
- **Average Recovery Time:** {profile_data['temporal_patterns']['recovery_patterns'].get('avg_recovery_time_ms', 0) / (60*1000):.1f} minutes

---
*Report generated by Model Fragility Analyzer v1.0*
"""
        
        return report


# CLI interface
def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Model Fragility Analyzer')
    parser.add_argument('--model-name', type=str, required=True, help='Model name to analyze')
    parser.add_argument('--model-kind', type=str, help='Model kind to analyze')
    parser.add_argument('--report', action='store_true', help='Generate fragility report')
    parser.add_argument('--cluster', action='store_true', help='Cluster failure patterns')
    parser.add_argument('--warnings', action='store_true', help='Show early warnings')
    
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO)
    analyzer = ModelFragilityAnalyzer()
    
    if args.report:
        report = analyzer.get_fragility_report(args.model_name, args.model_kind)
        print(report)
        return
    
    if args.cluster:
        clusters = analyzer.cluster_failure_patterns(args.model_name)
        print(f"\nFailure Pattern Clusters for {args.model_name}:")
        for cluster in clusters:
            print(f"\nCluster {cluster.cluster_id}:")
            print(f"  Failure Modes: {[m.value for m in cluster.failure_modes]}")
            print(f"  Avg Fragility: {cluster.avg_fragility:.3f}")
            print(f"  Scenarios: {cluster.scenario_count}")
            print(f"  Common Triggers: {cluster.common_triggers}")
            print(f"  Recommended Fixes: {cluster.recommended_fixes}")
        return
    
    if args.warnings:
        con = connect()
        try:
            rows = con.execute("""
                SELECT warning_type, severity, message, created_ts_ms
                FROM early_warnings
                WHERE model_name = ? AND acknowledged = 0
                ORDER BY created_ts_ms DESC
            """, (args.model_name,)).fetchall()
            
            if not rows:
                print(f"No unacknowledged warnings for {args.model_name}")
                return
            
            print(f"\nEarly Warnings for {args.model_name}:")
            for row in rows:
                date = datetime.fromtimestamp(row[3]/1000).strftime('%Y-%m-%d %H:%M')
                print(f"[{date}] {row[1].upper()} - {row[0]}: {row[2]}")
        finally:
            con.close()
        return
    
    # Default: run fragility analysis
    profile = analyzer.analyze_model_fragility(
        model_name=args.model_name,
        model_kind=args.model_kind or "default",
        model_ts_ms=int(datetime.now().timestamp() * 1000)
    )
    
    print(f"Fragility analysis complete for {args.model_name}")
    print(f"Overall fragility: {profile.overall_fragility:.3f}")
    print(f"Top failure modes: {[m.value for m in profile.failure_modes[:3]]}")


if __name__ == "__main__":
    main()
