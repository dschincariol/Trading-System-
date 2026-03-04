# adversarial_scenario_generator.py
"""
Adversarial AI Stress-Testing System for Financial Models

Generates synthetic market scenarios to test model robustness under extreme but plausible conditions.
Offline only - never connects to live trading execution.

Scenario Types:
- Market crashes (rapid price declines)
- Regime shifts (volatility/spread changes)
- Liquidity droughts (order book thinning)
- News shocks (sentiment-driven volatility)
- Correlation breakdowns
- Flash crashes
"""

import os
import json
import math
import random
import logging
import numpy as np
from dataclasses import dataclass, asdict
from typing import Dict, List, Any, Optional, Tuple, Union
from datetime import datetime, timedelta
from enum import Enum

from engine.storage import connect, init_db
from engine.regime_stack import compute_regime_vector


class ScenarioType(Enum):
    MARKET_CRASH = "market_crash"
    REGIME_SHIFT = "regime_shift"
    LIQUIDITY_DROUGHT = "liquidity_drought"
    NEWS_SHOCK = "news_shock"
    CORRELATION_BREAKDOWN = "correlation_breakdown"
    FLASH_CRASH = "flash_crash"
    VOLATILITY_SPIKE = "volatility_spike"
    SPREAD_WIDENING = "spread_widening"


@dataclass
class ScenarioParameters:
    """Parameters defining a stress test scenario"""
    scenario_type: ScenarioType
    severity: float  # 0.0 to 1.0, where 1.0 is most severe
    duration_minutes: int
    affected_symbols: List[str]
    start_ts_ms: int
    market_impact_z: float  # Expected market move in z-scores
    volatility_multiplier: float
    liquidity_multiplier: float
    correlation_shift: float
    confidence_dampening: float
    execution_delay_ms: int
    spread_widening_bps: float
    
    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result['scenario_type'] = self.scenario_type.value
        return result


@dataclass
class ScenarioResult:
    """Results from running a stress test scenario"""
    scenario_id: str
    parameters: ScenarioParameters
    baseline_metrics: Dict[str, Any]
    stress_metrics: Dict[str, Any]
    fragility_score: float
    failure_modes: List[str]
    recovery_time_ms: int
    max_drawdown_delta: float
    prediction_error_spike: float
    execution_cost_increase: float
    
    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result['parameters'] = self.parameters.to_dict()
        return result


class AdversarialScenarioGenerator:
    """Main class for generating and executing adversarial stress scenarios"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.scenario_templates = self._load_scenario_templates()
        self._ensure_tables()
    
    def _load_scenario_templates(self) -> Dict[ScenarioType, Dict[str, Any]]:
        """Load predefined scenario templates with realistic parameter ranges"""
        return {
            ScenarioType.MARKET_CRASH: {
                'severity_range': (0.3, 1.0),
                'duration_range': (5, 60),
                'market_impact_z_range': (-5.0, -2.0),
                'volatility_multiplier_range': (2.0, 8.0),
                'liquidity_multiplier_range': (0.1, 0.5),
                'correlation_shift_range': (0.3, 0.8),
                'confidence_dampening_range': (0.2, 0.7),
                'execution_delay_range': (100, 2000),
                'spread_widening_bps_range': (10, 100),
            },
            ScenarioType.REGIME_SHIFT: {
                'severity_range': (0.2, 0.8),
                'duration_range': (30, 240),
                'market_impact_z_range': (-1.5, 1.5),
                'volatility_multiplier_range': (1.5, 4.0),
                'liquidity_multiplier_range': (0.3, 0.8),
                'correlation_shift_range': (0.2, 0.6),
                'confidence_dampening_range': (0.1, 0.4),
                'execution_delay_range': (50, 500),
                'spread_widening_bps_range': (5, 30),
            },
            ScenarioType.LIQUIDITY_DROUGHT: {
                'severity_range': (0.4, 1.0),
                'duration_range': (10, 120),
                'market_impact_z_range': (-1.0, 0.5),
                'volatility_multiplier_range': (1.2, 3.0),
                'liquidity_multiplier_range': (0.05, 0.3),
                'correlation_shift_range': (0.1, 0.4),
                'confidence_dampening_range': (0.3, 0.6),
                'execution_delay_range': (200, 5000),
                'spread_widening_bps_range': (20, 150),
            },
            ScenarioType.NEWS_SHOCK: {
                'severity_range': (0.2, 0.9),
                'duration_range': (2, 30),
                'market_impact_z_range': (-3.0, 3.0),
                'volatility_multiplier_range': (2.5, 10.0),
                'liquidity_multiplier_range': (0.2, 0.7),
                'correlation_shift_range': (0.4, 0.9),
                'confidence_dampening_range': (0.4, 0.8),
                'execution_delay_range': (50, 1000),
                'spread_widening_bps_range': (15, 80),
            },
            ScenarioType.FLASH_CRASH: {
                'severity_range': (0.6, 1.0),
                'duration_range': (1, 10),
                'market_impact_z_range': (-8.0, -3.0),
                'volatility_multiplier_range': (5.0, 15.0),
                'liquidity_multiplier_range': (0.01, 0.2),
                'correlation_shift_range': (0.6, 1.0),
                'confidence_dampening_range': (0.6, 0.9),
                'execution_delay_range': (500, 3000),
                'spread_widening_bps_range': (50, 200),
            },
            ScenarioType.VOLATILITY_SPIKE: {
                'severity_range': (0.3, 0.8),
                'duration_range': (5, 45),
                'market_impact_z_range': (-2.0, 2.0),
                'volatility_multiplier_range': (3.0, 12.0),
                'liquidity_multiplier_range': (0.3, 0.6),
                'correlation_shift_range': (0.2, 0.5),
                'confidence_dampening_range': (0.3, 0.7),
                'execution_delay_range': (100, 800),
                'spread_widening_bps_range': (10, 60),
            },
            ScenarioType.SPREAD_WIDENING: {
                'severity_range': (0.2, 0.7),
                'duration_range': (15, 180),
                'market_impact_z_range': (-0.5, 0.5),
                'volatility_multiplier_range': (1.3, 3.5),
                'liquidity_multiplier_range': (0.4, 0.8),
                'correlation_shift_range': (0.1, 0.3),
                'confidence_dampening_range': (0.2, 0.5),
                'execution_delay_range': (50, 400),
                'spread_widening_bps_range': (25, 120),
            },
        }
    
    def _ensure_tables(self):
        """Create database tables for stress testing results"""
        schema = """
        CREATE TABLE IF NOT EXISTS adversarial_scenarios (
            id TEXT PRIMARY KEY,
            created_ts_ms INTEGER NOT NULL,
            parameters_json TEXT NOT NULL,
            result_json TEXT,
            status TEXT DEFAULT 'pending'
        );
        
        CREATE TABLE IF NOT EXISTS stress_test_summary (
            test_batch_id TEXT PRIMARY KEY,
            created_ts_ms INTEGER NOT NULL,
            scenario_count INTEGER NOT NULL,
            pass_count INTEGER NOT NULL,
            fail_count INTEGER NOT NULL,
            avg_fragility_score REAL NOT NULL,
            worst_failure_mode TEXT,
            summary_json TEXT NOT NULL
        );
        
        CREATE INDEX IF NOT EXISTS idx_scenarios_created ON adversarial_scenarios(created_ts_ms);
        CREATE INDEX IF NOT EXISTS idx_scenarios_status ON adversarial_scenarios(status);
        """
        
        con = connect()
        try:
            con.executescript(schema)
            con.commit()
        finally:
            con.close()
    
    def generate_scenario(
        self,
        scenario_type: ScenarioType,
        severity: Optional[float] = None,
        affected_symbols: Optional[List[str]] = None,
        start_ts_ms: Optional[int] = None
    ) -> ScenarioParameters:
        """Generate a single stress test scenario with realistic parameters"""
        
        template = self.scenario_templates[scenario_type]
        
        # Sample parameters within realistic ranges
        if severity is None:
            severity = random.uniform(*template['severity_range'])
        
        duration = random.randint(*template['duration_range'])
        market_impact_z = random.uniform(*template['market_impact_z_range'])
        vol_mult = random.uniform(*template['volatility_multiplier_range'])
        liq_mult = random.uniform(*template['liquidity_multiplier_range'])
        corr_shift = random.uniform(*template['correlation_shift_range'])
        conf_damp = random.uniform(*template['confidence_dampening_range'])
        exec_delay = random.randint(*template['execution_delay_range'])
        spread_widen = random.uniform(*template['spread_widening_bps_range'])
        
        # Get default symbols if not specified
        if affected_symbols is None:
            affected_symbols = self._get_liquid_symbols()
        
        # Set start time if not specified
        if start_ts_ms is None:
            start_ts_ms = int((datetime.now() - timedelta(days=7)).timestamp() * 1000)
        
        return ScenarioParameters(
            scenario_type=scenario_type,
            severity=float(severity),
            duration_minutes=int(duration),
            affected_symbols=affected_symbols,
            start_ts_ms=int(start_ts_ms),
            market_impact_z=float(market_impact_z),
            volatility_multiplier=float(vol_mult),
            liquidity_multiplier=float(liq_mult),
            correlation_shift=float(corr_shift),
            confidence_dampening=float(conf_damp),
            execution_delay_ms=int(exec_delay),
            spread_widening_bps=float(spread_widen)
        )
    
    def _get_liquid_symbols(self) -> List[str]:
        """Get a list of liquid symbols for stress testing"""
        con = connect()
        try:
            # Get symbols with recent alerts/activity
            rows = con.execute("""
                SELECT DISTINCT symbol
                FROM alerts
                WHERE ts_ms > ? AND confidence > 0.5
                ORDER BY COUNT(*) DESC
                LIMIT 20
            """, ((int(datetime.now().timestamp() * 1000) - 30 * 86400 * 1000),)).fetchall()
            
            symbols = [row[0] for row in rows if row[0]]
            return symbols if symbols else ['AAPL', 'MSFT', 'SPY', 'QQQ', 'GLD']
        finally:
            con.close()
    
    def generate_scenario_suite(
        self,
        scenario_types: Optional[List[ScenarioType]] = None,
        scenarios_per_type: int = 3,
        severity_levels: List[float] = None
    ) -> List[ScenarioParameters]:
        """Generate a comprehensive suite of stress test scenarios"""
        
        if scenario_types is None:
            scenario_types = list(ScenarioType)
        
        if severity_levels is None:
            severity_levels = [0.3, 0.6, 0.9]  # Low, medium, high severity
        
        scenarios = []
        
        for scenario_type in scenario_types:
            for severity in severity_levels:
                for _ in range(scenarios_per_type):
                    # Add some randomness to severity
                    actual_severity = severity + random.uniform(-0.1, 0.1)
                    actual_severity = max(0.1, min(1.0, actual_severity))
                    
                    scenario = self.generate_scenario(
                        scenario_type=scenario_type,
                        severity=actual_severity
                    )
                    scenarios.append(scenario)
        
        return scenarios
    
    def apply_scenario_to_backtest(
        self,
        scenario: ScenarioParameters,
        baseline_backtest_id: Optional[int] = None
    ) -> ScenarioResult:
        """Apply a stress scenario to the backtesting pipeline"""
        
        scenario_id = f"{scenario.scenario_type.value}_{scenario.start_ts_ms}_{int(severity * 100)}"
        
        # Store scenario in database
        con = connect()
        try:
            con.execute("""
                INSERT INTO adversarial_scenarios (id, created_ts_ms, parameters_json, status)
                VALUES (?, ?, ?, ?)
            """, (scenario_id, int(datetime.now().timestamp() * 1000), 
                  json.dumps(scenario.to_dict()), 'running'))
            con.commit()
        finally:
            con.close()
        
        # Get baseline metrics
        baseline_metrics = self._get_baseline_metrics(baseline_backtest_id)
        
        # Apply stress conditions to backtest environment
        stress_metrics = self._run_stressed_backtest(scenario)
        
        # Calculate fragility and failure modes
        fragility_score = self._calculate_fragility_score(baseline_metrics, stress_metrics)
        failure_modes = self._identify_failure_modes(baseline_metrics, stress_metrics)
        recovery_time = self._estimate_recovery_time(stress_metrics)
        drawdown_delta = self._calculate_drawdown_delta(baseline_metrics, stress_metrics)
        prediction_error = self._calculate_prediction_error_spike(baseline_metrics, stress_metrics)
        cost_increase = self._calculate_execution_cost_increase(baseline_metrics, stress_metrics)
        
        result = ScenarioResult(
            scenario_id=scenario_id,
            parameters=scenario,
            baseline_metrics=baseline_metrics,
            stress_metrics=stress_metrics,
            fragility_score=fragility_score,
            failure_modes=failure_modes,
            recovery_time_ms=recovery_time,
            max_drawdown_delta=drawdown_delta,
            prediction_error_spike=prediction_error,
            execution_cost_increase=cost_increase
        )
        
        # Update scenario with results
        con = connect()
        try:
            con.execute("""
                UPDATE adversarial_scenarios 
                SET result_json=?, status=?
                WHERE id=?
            """, (json.dumps(result.to_dict()), 'completed', scenario_id))
            con.commit()
        finally:
            con.close()
        
        return result
    
    def _get_baseline_metrics(self, backtest_id: Optional[int]) -> Dict[str, Any]:
        """Get baseline metrics from a recent backtest run"""
        con = connect()
        try:
            if backtest_id:
                row = con.execute("""
                    SELECT metrics_json FROM portfolio_bt_runs WHERE id=?
                """, (backtest_id,)).fetchone()
                if row:
                    return json.loads(row[0])
            
            # Get most recent successful backtest
            row = con.execute("""
                SELECT metrics_json FROM portfolio_bt_runs 
                WHERE metrics_json NOT NULL
                ORDER BY ts_ms DESC LIMIT 1
            """).fetchone()
            
            return json.loads(row[0]) if row else {}
        finally:
            con.close()
    
    def _run_stressed_backtest(self, scenario: ScenarioParameters) -> Dict[str, Any]:
        """Run backtest with stress conditions applied"""
        from engine.research.offline_backtest_eval import run_portfolio_backtest
        
        # Environment overrides for stress conditions
        env_overrides = {
            'BT_DAYS': str(max(1, scenario.duration_minutes // 60)),  # Convert to days
            'PORTFOLIO_MIN_CONF': str(max(0.1, 0.5 - scenario.confidence_dampening)),
            'PORTFOLIO_MAX_POSITIONS': str(max(1, int(10 * scenario.liquidity_multiplier))),
            'STRESS_VOLATILITY_MULT': str(scenario.volatility_multiplier),
            'STRESS_LIQUIDITY_MULT': str(scenario.liquidity_multiplier),
            'STRESS_SPREAD_WIDENING': str(scenario.spread_widening_bps),
            'STRESS_EXECUTION_DELAY': str(scenario.execution_delay_ms),
            'STRESS_MARKET_IMPACT_Z': str(scenario.market_impact_z),
        }
        
        # Run stressed backtest
        result = run_portfolio_backtest(env_overrides=env_overrides)
        
        if result.ok:
            return result.metrics
        else:
            self.logger.error(f"Stressed backtest failed: {result.error}")
            return {"error": result.error, "ok": False}
    
    def _calculate_fragility_score(
        self, 
        baseline: Dict[str, Any], 
        stressed: Dict[str, Any]
    ) -> float:
        """Calculate overall fragility score (0-1, higher = more fragile)"""
        
        def safe_get(metrics, key, default=0.0):
            try:
                return float(metrics.get(key, default))
            except (ValueError, TypeError):
                return float(default)
        
        # Key degradation metrics
        base_return = safe_get(baseline, 'total_return', 0.0)
        stress_return = safe_get(stressed, 'total_return', 0.0)
        return_degradation = max(0, (base_return - stress_return) / max(abs(base_return), 0.01))
        
        base_sharpe = safe_get(baseline, 'sharpe_simple', 0.0)
        stress_sharpe = safe_get(stressed, 'sharpe_simple', 0.0)
        sharpe_degradation = max(0, (base_sharpe - stress_sharpe) / max(abs(base_sharpe), 0.01))
        
        base_dd = abs(safe_get(baseline, 'max_drawdown', 0.0))
        stress_dd = abs(safe_get(stressed, 'max_drawdown', 0.0))
        dd_increase = max(0, (stress_dd - base_dd) / max(base_dd, 0.01))
        
        # Weighted combination
        fragility = (
            0.4 * return_degradation +
            0.3 * sharpe_degradation +
            0.3 * dd_increase
        )
        
        return min(1.0, max(0.0, fragility))
    
    def _identify_failure_modes(
        self, 
        baseline: Dict[str, Any], 
        stressed: Dict[str, Any]
    ) -> List[str]:
        """Identify specific failure modes from the stress test"""
        
        failure_modes = []
        
        def safe_get(metrics, key, default=0.0):
            try:
                return float(metrics.get(key, default))
            except (ValueError, TypeError):
                return float(default)
        
        # Return degradation
        base_ret = safe_get(baseline, 'total_return', 0.0)
        stress_ret = safe_get(stressed, 'total_return', 0.0)
        if stress_ret < base_ret * 0.7:  # >30% return loss
            failure_modes.append("significant_return_degradation")
        
        # Drawdown spike
        base_dd = abs(safe_get(baseline, 'max_drawdown', 0.0))
        stress_dd = abs(safe_get(stressed, 'max_drawdown', 0.0))
        if stress_dd > base_dd * 2.0:  # Drawdown doubled
            failure_modes.append("excessive_drawdown")
        
        # Volatility explosion
        base_vol = safe_get(baseline, 'ret_volatility', 0.0)
        stress_vol = safe_get(stressed, 'ret_volatility', 0.0)
        if stress_vol > base_vol * 3.0:  # Volatility tripled
            failure_modes.append("volatility_explosion")
        
        # Execution cost spike
        base_cost = safe_get(baseline, 'total_exec_cost', 0.0)
        stress_cost = safe_get(stressed, 'total_exec_cost', 0.0)
        if stress_cost > base_cost * 2.5:  # Costs more than doubled
            failure_modes.append("execution_cost_spike")
        
        # Model prediction errors (if available)
        if 'rmse_net' in stressed:
            stress_rmse = safe_get(stressed, 'rmse_net', 0.0)
            if stress_rmse > 2.0:  # High prediction error
                failure_modes.append("prediction_accuracy_loss")
        
        return failure_modes
    
    def _estimate_recovery_time(self, stressed_metrics: Dict[str, Any]) -> int:
        """Estimate recovery time in milliseconds based on drawdown profile"""
        # Simplified: use drawdown magnitude as proxy for recovery time
        max_dd = abs(stressed_metrics.get('max_drawdown', 0.0))
        
        # Rough heuristic: larger drawdowns take longer to recover
        if max_dd < 0.05:
            return 5 * 60 * 1000  # 5 minutes
        elif max_dd < 0.15:
            return 30 * 60 * 1000  # 30 minutes
        elif max_dd < 0.30:
            return 2 * 60 * 60 * 1000  # 2 hours
        else:
            return 24 * 60 * 60 * 1000  # 24+ hours
    
    def _calculate_drawdown_delta(
        self, 
        baseline: Dict[str, Any], 
        stressed: Dict[str, Any]
    ) -> float:
        """Calculate increase in maximum drawdown"""
        base_dd = abs(baseline.get('max_drawdown', 0.0))
        stress_dd = abs(stressed.get('max_drawdown', 0.0))
        return stress_dd - base_dd
    
    def _calculate_prediction_error_spike(
        self, 
        baseline: Dict[str, Any], 
        stressed: Dict[str, Any]
    ) -> float:
        """Calculate spike in prediction errors"""
        base_rmse = baseline.get('rmse_net', baseline.get('rmse', 0.0))
        stress_rmse = stressed.get('rmse_net', stressed.get('rmse', 0.0))
        return float(stress_rmse) - float(base_rmse)
    
    def _calculate_execution_cost_increase(
        self, 
        baseline: Dict[str, Any], 
        stressed: Dict[str, Any]
    ) -> float:
        """Calculate increase in execution costs"""
        base_cost = baseline.get('total_exec_cost', 0.0)
        stress_cost = stressed.get('total_exec_cost', 0.0)
        return float(stress_cost) - float(base_cost)
    
    def run_stress_test_suite(
        self,
        scenarios: Optional[List[ScenarioParameters]] = None,
        pass_threshold: float = 0.7
    ) -> Dict[str, Any]:
        """Run a complete stress test suite and generate summary report"""
        
        if scenarios is None:
            scenarios = self.generate_scenario_suite()
        
        test_batch_id = f"stress_test_{int(datetime.now().timestamp() * 1000)}"
        results = []
        
        self.logger.info(f"Running stress test suite with {len(scenarios)} scenarios")
        
        for scenario in scenarios:
            try:
                result = self.apply_scenario_to_backtest(scenario)
                results.append(result)
                self.logger.info(f"Completed scenario {result.scenario_id}: fragility={result.fragility_score:.3f}")
            except Exception as e:
                self.logger.error(f"Failed to run scenario {scenario.scenario_type}: {e}")
                continue
        
        # Calculate summary statistics
        pass_count = sum(1 for r in results if r.fragility_score <= (1.0 - pass_threshold))
        fail_count = len(results) - pass_count
        avg_fragility = sum(r.fragility_score for r in results) / len(results) if results else 0.0
        
        # Find worst failure mode
        all_failure_modes = []
        for r in results:
            all_failure_modes.extend(r.failure_modes)
        worst_failure = max(set(all_failure_modes), key=all_failure_modes.count) if all_failure_modes else None
        
        summary = {
            'test_batch_id': test_batch_id,
            'created_ts_ms': int(datetime.now().timestamp() * 1000),
            'scenario_count': len(scenarios),
            'pass_count': pass_count,
            'fail_count': fail_count,
            'pass_rate': pass_count / len(results) if results else 0.0,
            'avg_fragility_score': avg_fragility,
            'worst_failure_mode': worst_failure,
            'pass_threshold': pass_threshold,
            'results': [r.to_dict() for r in results]
        }
        
        # Store summary
        con = connect()
        try:
            con.execute("""
                INSERT INTO stress_test_summary 
                (test_batch_id, created_ts_ms, scenario_count, pass_count, fail_count, 
                 avg_fragility_score, worst_failure_mode, summary_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (test_batch_id, summary['created_ts_ms'], summary['scenario_count'],
                  summary['pass_count'], summary['fail_count'], summary['avg_fragility_score'],
                  summary['worst_failure_mode'], json.dumps(summary)))
            con.commit()
        finally:
            con.close()
        
        self.logger.info(f"Stress test complete: {pass_count}/{len(results)} passed, avg fragility={avg_fragility:.3f}")
        
        return summary
    
    def generate_risk_report(self, test_batch_id: str) -> str:
        """Generate a structured risk report for a stress test batch"""
        
        con = connect()
        try:
            row = con.execute("""
                SELECT summary_json FROM stress_test_summary WHERE test_batch_id=?
            """, (test_batch_id,)).fetchone()
            
            if not row:
                return "Error: Test batch not found"
            
            summary = json.loads(row[0])
        finally:
            con.close()
        
        # Generate structured report
        report = f"""
# Adversarial Stress Test Risk Report

**Test Batch ID:** {test_batch_id}
**Generated:** {datetime.fromtimestamp(summary['created_ts_ms']/1000).strftime('%Y-%m-%d %H:%M:%S')}
**Scenarios Tested:** {summary['scenario_count']}

## Executive Summary
- **Pass Rate:** {summary['pass_rate']:.1%} ({summary['pass_count']}/{summary['scenario_count']})
- **Average Fragility Score:** {summary['avg_fragility_score']:.3f}
- **Worst Failure Mode:** {summary.get('worst_failure_mode', 'None')}
- **Overall Status:** {'PASS' if summary['pass_rate'] >= summary['pass_threshold'] else 'FAIL'}

## Failure Analysis
"""
        
        # Count failure modes
        failure_counts = {}
        for result in summary['results']:
            for mode in result['failure_modes']:
                failure_counts[mode] = failure_counts.get(mode, 0) + 1
        
        if failure_counts:
            report += "### Most Common Failure Modes:\n"
            for mode, count in sorted(failure_counts.items(), key=lambda x: x[1], reverse=True):
                report += f"- **{mode}:** {count} scenarios\n"
        else:
            report += "No critical failure modes detected.\n"
        
        # Worst scenarios
        worst_scenarios = sorted(summary['results'], key=lambda x: x['fragility_score'], reverse=True)[:5]
        
        report += f"""
## Worst Performing Scenarios
| Scenario ID | Type | Severity | Fragility | Key Failures |
|-------------|------|----------|-----------|--------------|
"""
        
        for scenario in worst_scenarios:
            params = scenario['parameters']
            failures = ', '.join(scenario['failure_modes'][:2])  # Show top 2 failures
            report += f"| {scenario['scenario_id']} | {params['scenario_type']} | {params['severity']:.2f} | {scenario['fragility_score']:.3f} | {failures} |\n"
        
        # Recommendations
        report += f"""
## Recommendations

"""
        
        if summary['pass_rate'] < summary['pass_threshold']:
            report += "### ⚠️ CRITICAL ISSUES FOUND\n"
            report += "The model failed to meet the minimum robustness threshold. Immediate action required:\n"
            
            if 'excessive_drawdown' in failure_counts:
                report += "- Implement stronger position sizing limits\n"
                report += "- Add dynamic drawdown controls\n"
            
            if 'significant_return_degradation' in failure_counts:
                report += "- Review model feature engineering\n"
                report += "- Consider ensemble methods for stability\n"
            
            if 'execution_cost_spike' in failure_counts:
                report += "- Optimize execution algorithms\n"
                report += "- Implement adaptive spread monitoring\n"
        else:
            report += "### ✅ MODEL ROBUSTNESS ACCEPTABLE\n"
            report += "The model demonstrates acceptable resilience to stress scenarios.\n"
            report += "Continue monitoring with regular stress tests.\n"
        
        report += f"""
## Technical Details
- **Pass Threshold:** {summary['pass_threshold']:.1%}
- **Test Duration:** {summary['scenario_count']} scenarios
- **Generated:** Adversarial AI Stress Testing System v1.0

---
*This report was generated automatically by the adversarial stress testing system.*
*All tests were conducted offline with no connection to live trading systems.*
"""
        
        return report


# CLI interface for standalone execution
def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Adversarial AI Stress Testing System')
    parser.add_argument('--scenario-type', type=str, help='Specific scenario type to test')
    parser.add_argument('--severity', type=float, help='Severity level (0.0-1.0)')
    parser.add_argument('--scenarios', type=int, default=3, help='Scenarios per type')
    parser.add_argument('--pass-threshold', type=float, default=0.7, help='Pass rate threshold')
    parser.add_argument('--report-only', type=str, help='Generate report for existing test batch')
    
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO)
    
    generator = AdversarialScenarioGenerator()
    
    if args.report_only:
        report = generator.generate_risk_report(args.report_only)
        print(report)
        return
    
    # Generate and run stress test suite
    if args.scenario_type:
        scenario_types = [ScenarioType(args.scenario_type)]
    else:
        scenario_types = None
    
    scenarios = generator.generate_scenario_suite(
        scenario_types=scenario_types,
        scenarios_per_type=args.scenarios
    )
    
    results = generator.run_stress_test_suite(
        scenarios=scenarios,
        pass_threshold=args.pass_threshold
    )
    
    # Generate and print report
    report = generator.generate_risk_report(results['test_batch_id'])
    print(report)
    
    # Exit with appropriate code
    exit_code = 0 if results['pass_rate'] >= args.pass_threshold else 1
    exit(exit_code)


if __name__ == "__main__":
    main()
