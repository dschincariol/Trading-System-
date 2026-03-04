# ci_stress_testing.py
"""
CI-Style Pass/Fail Thresholds for Adversarial Stress Testing

This module implements continuous integration style automated testing with
pass/fail thresholds for model robustness. It provides:

- Automated gate enforcement
- Configurable pass/fail criteria
- Multi-stage validation pipeline
- Automated rollback capabilities
- Integration with existing CI/CD systems
- Notification and alerting
"""

import os
import json
import logging
import subprocess
import smtplib
from dataclasses import dataclass, asdict
from typing import Dict, List, Any, Optional, Tuple, Union
from datetime import datetime, timedelta
from enum import Enum
from email.mime.text import MimeText
from email.mime.multipart import MimeMultipart

from engine.storage import connect
from engine.research.adversarial_scenario_generator import ScenarioType
from engine.research.stress_test_integration import StressTestIntegration
from engine.research.model_fragility_analyzer import ModelFragilityAnalyzer
from engine.research.risk_reporting_system import RiskReportingSystem, ReportType


class GateStatus(Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"
    BLOCKED = "blocked"


class NotificationLevel(Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class GateResult:
    """Result of a CI gate execution"""
    gate_name: str
    status: GateStatus
    score: Optional[float]
    threshold: Optional[float]
    details: Dict[str, Any]
    execution_time_ms: int
    error_message: Optional[str]
    
    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result['status'] = self.status.value
        return result


@dataclass
class PipelineResult:
    """Result of complete CI pipeline execution"""
    pipeline_id: str
    model_name: str
    model_kind: Optional[str]
    model_version: str
    overall_status: GateStatus
    gate_results: List[GateResult]
    start_ts_ms: int
    end_ts_ms: int
    total_execution_time_ms: int
    triggered_by: str
    environment: str
    
    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result['overall_status'] = self.overall_status.value
        result['gate_results'] = [gate.to_dict() for gate in self.gate_results]
        return result


@dataclass
class ThresholdConfig:
    """Configuration for pass/fail thresholds"""
    gate_name: str
    enabled: bool
    pass_threshold: float
    fail_threshold: float
    warning_threshold: Optional[float]
    required_scenarios: int
    scenario_types: List[str]
    severity_levels: List[float]
    timeout_minutes: int
    retry_count: int
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CIStressTesting:
    """CI-style stress testing system with pass/fail thresholds"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self._ensure_tables()
        self.integration = StressTestIntegration()
        self.analyzer = ModelFragilityAnalyzer()
        self.reporter = RiskReportingSystem()
        self.default_thresholds = self._initialize_default_thresholds()
    
    def _ensure_tables(self):
        """Create database tables for CI stress testing"""
        schema = """
        CREATE TABLE IF NOT EXISTS ci_pipeline_runs (
            pipeline_id TEXT PRIMARY KEY,
            model_name TEXT NOT NULL,
            model_kind TEXT,
            model_version TEXT NOT NULL,
            overall_status TEXT NOT NULL,
            gate_results_json TEXT NOT NULL,
            start_ts_ms INTEGER NOT NULL,
            end_ts_ms INTEGER,
            total_execution_time_ms INTEGER,
            triggered_by TEXT NOT NULL,
            environment TEXT NOT NULL,
            created_ts_ms INTEGER NOT NULL
        );
        
        CREATE TABLE IF NOT EXISTS ci_threshold_configs (
            gate_name TEXT PRIMARY KEY,
            config_json TEXT NOT NULL,
            updated_ts_ms INTEGER NOT NULL,
            updated_by TEXT
        );
        
        CREATE TABLE IF NOT EXISTS ci_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pipeline_id TEXT NOT NULL,
            notification_level TEXT NOT NULL,
            recipient TEXT NOT NULL,
            message TEXT NOT NULL,
            sent_ts_ms INTEGER,
            created_ts_ms INTEGER NOT NULL,
            FOREIGN KEY (pipeline_id) REFERENCES ci_pipeline_runs(pipeline_id)
        );
        
        CREATE TABLE IF NOT EXISTS ci_rollback_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pipeline_id TEXT NOT NULL,
            model_name TEXT NOT NULL,
            model_kind TEXT,
            from_version TEXT NOT NULL,
            to_version TEXT NOT NULL,
            reason TEXT NOT NULL,
            rollback_ts_ms INTEGER NOT NULL,
            FOREIGN KEY (pipeline_id) REFERENCES ci_pipeline_runs(pipeline_id)
        );
        
        CREATE INDEX IF NOT EXISTS idx_pipeline_runs_model ON ci_pipeline_runs(model_name, start_ts_ms);
        CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status ON ci_pipeline_runs(overall_status);
        CREATE INDEX IF NOT EXISTS idx_notifications_pipeline ON ci_notifications(pipeline_id);
        """
        
        con = connect()
        try:
            con.executescript(schema)
            con.commit()
        finally:
            con.close()
    
    def _initialize_default_thresholds(self) -> Dict[str, ThresholdConfig]:
        """Initialize default threshold configurations"""
        return {
            'pre_promotion_stress_test': ThresholdConfig(
                gate_name='pre_promotion_stress_test',
                enabled=True,
                pass_threshold=0.8,  # 80% pass rate
                fail_threshold=0.6,   # 60% fail threshold
                warning_threshold=0.7,
                required_scenarios=15,
                scenario_types=['market_crash', 'regime_shift', 'liquidity_drought', 'news_shock'],
                severity_levels=[0.6, 0.8, 0.95],
                timeout_minutes=30,
                retry_count=2
            ),
            'fragility_analysis': ThresholdConfig(
                gate_name='fragility_analysis',
                enabled=True,
                pass_threshold=0.3,  # Max 0.3 fragility score
                fail_threshold=0.7,   # 0.7+ fragility is failure
                warning_threshold=0.5,
                required_scenarios=10,
                scenario_types=['market_crash', 'volatility_spike', 'liquidity_drought'],
                severity_levels=[0.4, 0.7, 0.9],
                timeout_minutes=15,
                retry_count=1
            ),
            'execution_cost_validation': ThresholdConfig(
                gate_name='execution_cost_validation',
                enabled=True,
                pass_threshold=2.0,  # Max 2x cost increase
                fail_threshold=5.0,   # 5x+ cost increase is failure
                warning_threshold=3.0,
                required_scenarios=8,
                scenario_types=['spread_widening', 'liquidity_drought'],
                severity_levels=[0.5, 0.8],
                timeout_minutes=10,
                retry_count=1
            ),
            'prediction_accuracy_gate': ThresholdConfig(
                gate_name='prediction_accuracy_gate',
                enabled=True,
                pass_threshold=0.1,  # Max 0.1 RMSE increase
                fail_threshold=0.3,   # 0.3+ RMSE increase is failure
                warning_threshold=0.2,
                required_scenarios=12,
                scenario_types=['regime_shift', 'news_shock', 'volatility_spike'],
                severity_levels=[0.6, 0.9],
                timeout_minutes=20,
                retry_count=2
            ),
            'continuous_monitoring': ThresholdConfig(
                gate_name='continuous_monitoring',
                enabled=True,
                pass_threshold=0.7,  # 70% pass rate for continuous
                fail_threshold=0.5,   # 50% pass rate triggers alert
                warning_threshold=0.6,
                required_scenarios=6,
                scenario_types=['market_crash', 'volatility_spike'],
                severity_levels=[0.4, 0.7],
                timeout_minutes=15,
                retry_count=1
            )
        }
    
    def run_ci_pipeline(
        self,
        model_name: str,
        model_kind: Optional[str] = None,
        model_version: Optional[str] = None,
        triggered_by: str = "manual",
        environment: str = "production",
        gates: Optional[List[str]] = None
    ) -> PipelineResult:
        """Run complete CI stress testing pipeline"""
        
        pipeline_id = f"ci_{model_name}_{int(datetime.now().timestamp() * 1000)}"
        start_ts_ms = int(datetime.now().timestamp() * 1000)
        
        self.logger.info(f"Starting CI pipeline {pipeline_id} for {model_name}")
        
        if model_version is None:
            model_version = f"v{int(start_ts_ms)}"
        
        # Determine which gates to run
        if gates is None:
            gates = ['pre_promotion_stress_test', 'fragility_analysis', 'execution_cost_validation']
        
        gate_results = []
        
        for gate_name in gates:
            try:
                gate_result = self._run_gate(gate_name, model_name, model_kind, model_version)
                gate_results.append(gate_result)
                
                # Stop pipeline on critical failure
                if gate_result.status == GateStatus.FAILED and self._is_critical_gate(gate_name):
                    self.logger.warning(f"Critical gate {gate_name} failed, stopping pipeline")
                    break
                    
            except Exception as e:
                self.logger.error(f"Error running gate {gate_name}: {e}")
                gate_results.append(GateResult(
                    gate_name=gate_name,
                    status=GateStatus.ERROR,
                    score=None,
                    threshold=None,
                    details={'error': str(e)},
                    execution_time_ms=0,
                    error_message=str(e)
                ))
        
        end_ts_ms = int(datetime.now().timestamp() * 1000)
        total_time = end_ts_ms - start_ts_ms
        
        # Determine overall status
        overall_status = self._determine_overall_status(gate_results)
        
        pipeline_result = PipelineResult(
            pipeline_id=pipeline_id,
            model_name=model_name,
            model_kind=model_kind,
            model_version=model_version,
            overall_status=overall_status,
            gate_results=gate_results,
            start_ts_ms=start_ts_ms,
            end_ts_ms=end_ts_ms,
            total_execution_time_ms=total_time,
            triggered_by=triggered_by,
            environment=environment
        )
        
        # Store pipeline result
        self._store_pipeline_result(pipeline_result)
        
        # Send notifications
        self._send_notifications(pipeline_result)
        
        # Auto-rollback if needed
        if overall_status == GateStatus.FAILED and self._should_auto_rollback(pipeline_result):
            self._auto_rollback(pipeline_result)
        
        self.logger.info(f"CI pipeline {pipeline_id} completed with status: {overall_status.value}")
        
        return pipeline_result
    
    def _run_gate(
        self,
        gate_name: str,
        model_name: str,
        model_kind: Optional[str],
        model_version: str
    ) -> GateResult:
        """Run individual CI gate"""
        
        start_time = int(datetime.now().timestamp() * 1000)
        config = self._get_gate_config(gate_name)
        
        if not config.enabled:
            return GateResult(
                gate_name=gate_name,
                status=GateStatus.SKIPPED,
                score=None,
                threshold=None,
                details={'reason': 'Gate disabled'},
                execution_time_ms=0,
                error_message=None
            )
        
        try:
            if gate_name == 'pre_promotion_stress_test':
                result = self._run_stress_test_gate(model_name, model_kind, config)
            elif gate_name == 'fragility_analysis':
                result = self._run_fragility_gate(model_name, model_kind, config)
            elif gate_name == 'execution_cost_validation':
                result = self._run_execution_cost_gate(model_name, model_kind, config)
            elif gate_name == 'prediction_accuracy_gate':
                result = self._run_prediction_accuracy_gate(model_name, model_kind, config)
            elif gate_name == 'continuous_monitoring':
                result = self._run_continuous_monitoring_gate(model_name, config)
            else:
                result = GateResult(
                    gate_name=gate_name,
                    status=GateStatus.ERROR,
                    score=None,
                    threshold=None,
                    details={'error': f'Unknown gate: {gate_name}'},
                    execution_time_ms=0,
                    error_message=f'Unknown gate: {gate_name}'
                )
            
            execution_time = int(datetime.now().timestamp() * 1000) - start_time
            result.execution_time_ms = execution_time
            
            return result
            
        except Exception as e:
            execution_time = int(datetime.now().timestamp() * 1000) - start_time
            return GateResult(
                gate_name=gate_name,
                status=GateStatus.ERROR,
                score=None,
                threshold=None,
                details={'error': str(e)},
                execution_time_ms=execution_time,
                error_message=str(e)
            )
    
    def _run_stress_test_gate(
        self,
        model_name: str,
        model_kind: Optional[str],
        config: ThresholdConfig
    ) -> GateResult:
        """Run stress test validation gate"""
        
        # Get latest model timestamp
        model_ts_ms = self._get_model_timestamp(model_name, model_kind)
        
        # Run stress test
        stress_result = self.integration.run_pre_promotion_stress_test(
            model_name=model_name,
            model_kind=model_kind or "default",
            model_ts_ms=model_ts_ms
        )
        
        score = stress_result.get('pass_rate', 0.0)
        threshold = config.pass_threshold
        
        details = {
            'pass_rate': score,
            'scenario_count': stress_result.get('scenario_count', 0),
            'avg_fragility': stress_result.get('avg_fragility', 0),
            'test_batch_id': stress_result.get('test_batch_id'),
            'decision': stress_result.get('decision')
        }
        
        if score >= threshold:
            status = GateStatus.PASSED
        elif score <= config.fail_threshold:
            status = GateStatus.FAILED
        else:
            status = GateStatus.FAILED  # Warning threshold still fails in CI
        
        return GateResult(
            gate_name=config.gate_name,
            status=status,
            score=score,
            threshold=threshold,
            details=details,
            execution_time_ms=0,  # Set by caller
            error_message=None
        )
    
    def _run_fragility_gate(
        self,
        model_name: str,
        model_kind: Optional[str],
        config: ThresholdConfig
    ) -> GateResult:
        """Run fragility analysis gate"""
        
        # Get latest model timestamp
        model_ts_ms = self._get_model_timestamp(model_name, model_kind)
        
        # Analyze fragility
        profile = self.analyzer.analyze_model_fragility(
            model_name=model_name,
            model_kind=model_kind or "default",
            model_ts_ms=model_ts_ms
        )
        
        score = profile.overall_fragility
        threshold = config.pass_threshold
        
        details = {
            'overall_fragility': score,
            'dimension_scores': profile.dimension_scores,
            'failure_modes': profile.failure_modes,
            'remediation_priorities': len(profile.remediation_priorities)
        }
        
        # For fragility, lower is better
        if score <= threshold:
            status = GateStatus.PASSED
        elif score >= config.fail_threshold:
            status = GateStatus.FAILED
        else:
            status = GateStatus.FAILED
        
        return GateResult(
            gate_name=config.gate_name,
            status=status,
            score=score,
            threshold=threshold,
            details=details,
            execution_time_ms=0,
            error_message=None
        )
    
    def _run_execution_cost_gate(
        self,
        model_name: str,
        model_kind: Optional[str],
        config: ThresholdConfig
    ) -> GateResult:
        """Run execution cost validation gate"""
        
        # Get baseline and stressed execution costs
        baseline_cost = self._get_baseline_execution_cost(model_name, model_kind)
        stressed_cost = self._get_stressed_execution_cost(model_name, model_kind)
        
        if baseline_cost is None or stressed_cost is None:
            return GateResult(
                gate_name=config.gate_name,
                status=GateStatus.ERROR,
                score=None,
                threshold=None,
                details={'error': 'Could not retrieve execution cost data'},
                execution_time_ms=0,
                error_message='Missing execution cost data'
            )
        
        cost_multiplier = stressed_cost / max(baseline_cost, 0.001)
        threshold = config.pass_threshold
        
        details = {
            'baseline_cost': baseline_cost,
            'stressed_cost': stressed_cost,
            'cost_multiplier': cost_multiplier
        }
        
        if cost_multiplier <= threshold:
            status = GateStatus.PASSED
        elif cost_multiplier >= config.fail_threshold:
            status = GateStatus.FAILED
        else:
            status = GateStatus.FAILED
        
        return GateResult(
            gate_name=config.gate_name,
            status=status,
            score=cost_multiplier,
            threshold=threshold,
            details=details,
            execution_time_ms=0,
            error_message=None
        )
    
    def _run_prediction_accuracy_gate(
        self,
        model_name: str,
        model_kind: Optional[str],
        config: ThresholdConfig
    ) -> GateResult:
        """Run prediction accuracy gate"""
        
        # Get baseline and stressed prediction errors
        baseline_rmse = self._get_baseline_rmse(model_name, model_kind)
        stressed_rmse = self._get_stressed_rmse(model_name, model_kind)
        
        if baseline_rmse is None or stressed_rmse is None:
            return GateResult(
                gate_name=config.gate_name,
                status=GateStatus.ERROR,
                score=None,
                threshold=None,
                details={'error': 'Could not retrieve prediction accuracy data'},
                execution_time_ms=0,
                error_message='Missing prediction accuracy data'
            )
        
        rmse_increase = stressed_rmse - baseline_rmse
        threshold = config.pass_threshold
        
        details = {
            'baseline_rmse': baseline_rmse,
            'stressed_rmse': stressed_rmse,
            'rmse_increase': rmse_increase
        }
        
        if rmse_increase <= threshold:
            status = GateStatus.PASSED
        elif rmse_increase >= config.fail_threshold:
            status = GateStatus.FAILED
        else:
            status = GateStatus.FAILED
        
        return GateResult(
            gate_name=config.gate_name,
            status=status,
            score=rmse_increase,
            threshold=threshold,
            details=details,
            execution_time_ms=0,
            error_message=None
        )
    
    def _run_continuous_monitoring_gate(
        self,
        model_name: str,
        config: ThresholdConfig
    ) -> GateResult:
        """Run continuous monitoring gate"""
        
        # Run continuous stress test
        result = self.integration.run_continuous_stress_test(model_name)
        
        score = result.get('pass_rate', 0.0)
        threshold = config.pass_threshold
        
        details = {
            'pass_rate': score,
            'avg_fragility': result.get('avg_fragility', 0),
            'test_batch_id': result.get('test_batch_id')
        }
        
        if score >= threshold:
            status = GateStatus.PASSED
        elif score <= config.fail_threshold:
            status = GateStatus.FAILED
        else:
            status = GateStatus.FAILED
        
        return GateResult(
            gate_name=config.gate_name,
            status=status,
            score=score,
            threshold=threshold,
            details=details,
            execution_time_ms=0,
            error_message=None
        )
    
    def _get_gate_config(self, gate_name: str) -> ThresholdConfig:
        """Get gate configuration"""
        con = connect()
        try:
            row = con.execute("""
                SELECT config_json FROM ci_threshold_configs WHERE gate_name = ?
            """, (gate_name,)).fetchone()
            
            if row:
                config_data = json.loads(row[0])
                return ThresholdConfig(**config_data)
            else:
                # Use default configuration
                return self.default_thresholds.get(gate_name, ThresholdConfig(
                    gate_name=gate_name,
                    enabled=False,
                    pass_threshold=0.7,
                    fail_threshold=0.3,
                    warning_threshold=None,
                    required_scenarios=10,
                    scenario_types=[],
                    severity_levels=[],
                    timeout_minutes=15,
                    retry_count=1
                ))
        finally:
            con.close()
    
    def _get_model_timestamp(self, model_name: str, model_kind: Optional[str]) -> int:
        """Get latest model timestamp"""
        con = connect()
        try:
            query = """
                SELECT model_ts_ms FROM model_registry 
                WHERE model_name = ? AND stage = 'champion'
            """
            params = [model_name]
            
            if model_kind:
                query += " AND model_kind = ?"
                params.append(model_kind)
            
            query += " ORDER BY ts_ms DESC LIMIT 1"
            
            row = con.execute(query, params).fetchone()
            return int(row[0]) if row else int(datetime.now().timestamp() * 1000)
        finally:
            con.close()
    
    def _get_baseline_execution_cost(self, model_name: str, model_kind: Optional[str]) -> Optional[float]:
        """Get baseline execution cost"""
        con = connect()
        try:
            row = con.execute("""
                SELECT metrics_json FROM portfolio_bt_runs 
                WHERE metrics_json IS NOT NULL
                ORDER BY ts_ms DESC LIMIT 1
            """).fetchone()
            
            if row:
                metrics = json.loads(row[0])
                return metrics.get('total_exec_cost', 0.0)
            return None
        finally:
            con.close()
    
    def _get_stressed_execution_cost(self, model_name: str, model_kind: Optional[str]) -> Optional[float]:
        """Get stressed execution cost from recent stress tests"""
        con = connect()
        try:
            row = con.execute("""
                SELECT s.summary_json FROM stress_test_summary s
                JOIN stress_test_gates g ON s.test_batch_id = g.test_batch_id
                WHERE g.model_name = ? AND g.gate_type = 'pre_promotion'
                ORDER BY s.created_ts_ms DESC LIMIT 1
            """, (model_name,)).fetchone()
            
            if row:
                summary = json.loads(row[0])
                # Get average execution cost from stress test results
                results = summary.get('results', [])
                if results:
                    costs = [r.get('stress_metrics', {}).get('total_exec_cost', 0) for r in results]
                    return sum(costs) / len(costs) if costs else None
            return None
        finally:
            con.close()
    
    def _get_baseline_rmse(self, model_name: str, model_kind: Optional[str]) -> Optional[float]:
        """Get baseline RMSE"""
        con = connect()
        try:
            row = con.execute("""
                SELECT metrics_json FROM model_registry 
                WHERE model_name = ? AND stage = 'champion'
                ORDER BY ts_ms DESC LIMIT 1
            """, (model_name,)).fetchone()
            
            if row:
                metrics = json.loads(row[0])
                return metrics.get('rmse_net', metrics.get('rmse'))
            return None
        finally:
            con.close()
    
    def _get_stressed_rmse(self, model_name: str, model_kind: Optional[str]) -> Optional[float]:
        """Get stressed RMSE from recent stress tests"""
        con = connect()
        try:
            row = con.execute("""
                SELECT s.summary_json FROM stress_test_summary s
                JOIN stress_test_gates g ON s.test_batch_id = g.test_batch_id
                WHERE g.model_name = ? AND g.gate_type = 'pre_promotion'
                ORDER BY s.created_ts_ms DESC LIMIT 1
            """, (model_name,)).fetchone()
            
            if row:
                summary = json.loads(row[0])
                results = summary.get('results', [])
                if results:
                    rmses = [r.get('stress_metrics', {}).get('rmse_net', r.get('stress_metrics', {}).get('rmse')) for r in results]
                    valid_rmses = [r for r in rmses if r is not None]
                    return sum(valid_rmses) / len(valid_rmses) if valid_rmses else None
            return None
        finally:
            con.close()
    
    def _is_critical_gate(self, gate_name: str) -> bool:
        """Check if gate is critical (pipeline stops on failure)"""
        critical_gates = ['pre_promotion_stress_test', 'fragility_analysis']
        return gate_name in critical_gates
    
    def _determine_overall_status(self, gate_results: List[GateResult]) -> GateStatus:
        """Determine overall pipeline status"""
        
        if not gate_results:
            return GateStatus.ERROR
        
        # Check for errors
        if any(gate.status == GateStatus.ERROR for gate in gate_results):
            return GateStatus.ERROR
        
        # Check for failures
        if any(gate.status == GateStatus.FAILED for gate in gate_results):
            return GateStatus.FAILED
        
        # Check if any passed
        if any(gate.status == GateStatus.PASSED for gate in gate_results):
            return GateStatus.PASSED
        
        # All skipped
        return GateStatus.SKIPPED
    
    def _store_pipeline_result(self, result: PipelineResult):
        """Store pipeline result in database"""
        con = connect()
        try:
            con.execute("""
                INSERT INTO ci_pipeline_runs
                (pipeline_id, model_name, model_kind, model_version, overall_status,
                 gate_results_json, start_ts_ms, end_ts_ms, total_execution_time_ms,
                 triggered_by, environment, created_ts_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (result.pipeline_id, result.model_name, result.model_kind, result.model_version,
                  result.overall_status.value, json.dumps([g.to_dict() for g in result.gate_results]),
                  result.start_ts_ms, result.end_ts_ms, result.total_execution_time_ms,
                  result.triggered_by, result.environment, result.start_ts_ms))
            con.commit()
        finally:
            con.close()
    
    def _send_notifications(self, result: PipelineResult):
        """Send notifications based on pipeline result"""
        
        # Determine notification level
        if result.overall_status == GateStatus.FAILED:
            level = NotificationLevel.ERROR
        elif result.overall_status == GateStatus.ERROR:
            level = NotificationLevel.CRITICAL
        elif result.overall_status == GateStatus.PASSED:
            level = NotificationLevel.INFO
        else:
            level = NotificationLevel.WARNING
        
        # Get notification recipients
        recipients = self._get_notification_recipients(result.model_name, level)
        
        if not recipients:
            return
        
        # Generate notification message
        message = self._generate_notification_message(result, level)
        
        # Send notifications (email, Slack, etc.)
        for recipient in recipients:
            try:
                if recipient.startswith('email:'):
                    self._send_email_notification(recipient[6:], message, level)
                elif recipient.startswith('slack:'):
                    self._send_slack_notification(recipient[6:], message, level)
                
                # Log notification
                self._log_notification(result.pipeline_id, recipient, level, message)
                
            except Exception as e:
                self.logger.error(f"Failed to send notification to {recipient}: {e}")
    
    def _get_notification_recipients(self, model_name: str, level: NotificationLevel) -> List[str]:
        """Get notification recipients based on model and level"""
        # This would typically be configured per model/environment
        # For now, return default recipients
        base_recipients = [
            "email:model-team@company.com",
            "email:risk-team@company.com"
        ]
        
        if level in [NotificationLevel.ERROR, NotificationLevel.CRITICAL]:
            base_recipients.extend([
                "email:devops@company.com",
                "slack:#model-alerts"
            ])
        
        return base_recipients
    
    def _generate_notification_message(self, result: PipelineResult, level: NotificationLevel) -> str:
        """Generate notification message"""
        
        status_emoji = {
            GateStatus.PASSED: "✅",
            GateStatus.FAILED: "❌",
            GateStatus.ERROR: "🚨",
            GateStatus.SKIPPED: "⏭️"
        }
        
        message = f"""
{status_emoji.get(result.overall_status, '❓')} CI Stress Test Pipeline {result.overall_status.value.upper()}

**Model:** {result.model_name} ({result.model_kind or 'N/A'})
**Version:** {result.model_version}
**Pipeline ID:** {result.pipeline_id}
**Duration:** {result.total_execution_time_ms / 1000:.1f}s
**Triggered by:** {result.triggered_by}

**Gate Results:**
"""
        
        for gate in result.gate_results:
            gate_emoji = status_emoji.get(gate.status, '❓')
            message += f"\n{gate_emoji} {gate.gate_name}: {gate.status.value}"
            if gate.score is not None:
                message += f" (Score: {gate.score:.3f}, Threshold: {gate.threshold:.3f})"
        
        if result.overall_status in [GateStatus.FAILED, GateStatus.ERROR]:
            message += f"\n\n🔧 **Action Required:** Review failed gates and take corrective action."
        
        return message
    
    def _send_email_notification(self, email: str, message: str, level: NotificationLevel):
        """Send email notification"""
        # This would integrate with your email system
        # For now, just log the notification
        self.logger.info(f"Email notification to {email}: {level.value} - {message[:100]}...")
    
    def _send_slack_notification(self, channel: str, message: str, level: NotificationLevel):
        """Send Slack notification"""
        # This would integrate with Slack API
        # For now, just log the notification
        self.logger.info(f"Slack notification to {channel}: {level.value} - {message[:100]}...")
    
    def _log_notification(self, pipeline_id: str, recipient: str, level: NotificationLevel, message: str):
        """Log notification in database"""
        con = connect()
        try:
            con.execute("""
                INSERT INTO ci_notifications
                (pipeline_id, notification_level, recipient, message, created_ts_ms)
                VALUES (?, ?, ?, ?, ?)
            """, (pipeline_id, level.value, recipient, message, int(datetime.now().timestamp() * 1000)))
            con.commit()
        finally:
            con.close()
    
    def _should_auto_rollback(self, result: PipelineResult) -> bool:
        """Determine if auto-rollback should be triggered"""
        
        # Auto-rollback on critical failures
        critical_failures = [
            gate for gate in result.gate_results 
            if gate.status == GateStatus.FAILED and self._is_critical_gate(gate.name)
        ]
        
        return len(critical_failures) > 0 and result.environment == "production"
    
    def _auto_rollback(self, result: PipelineResult):
        """Perform automatic rollback"""
        
        self.logger.warning(f"Triggering auto-rollback for pipeline {result.pipeline_id}")
        
        # Get previous stable version
        previous_version = self._get_previous_stable_version(result.model_name, result.model_kind)
        
        if previous_version:
            # Perform rollback
            self._perform_rollback(result, previous_version)
        else:
            self.logger.error("No previous stable version found for rollback")
    
    def _get_previous_stable_version(self, model_name: str, model_kind: Optional[str]) -> Optional[str]:
        """Get previous stable model version"""
        con = connect()
        try:
            # Find last successful pipeline
            row = con.execute("""
                SELECT model_version FROM ci_pipeline_runs
                WHERE model_name = ? AND overall_status = 'passed'
                ORDER BY start_ts_ms DESC LIMIT 1
            """, (model_name,)).fetchone()
            
            return row[0] if row else None
        finally:
            con.close()
    
    def _perform_rollback(self, result: PipelineResult, previous_version: str):
        """Perform the actual rollback"""
        
        # Log rollback
        con = connect()
        try:
            con.execute("""
                INSERT INTO ci_rollback_log
                (pipeline_id, model_name, model_kind, from_version, to_version, reason, rollback_ts_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (result.pipeline_id, result.model_name, result.model_kind,
                  result.model_version, previous_version,
                  f"Auto-rollback due to CI failure: {result.pipeline_id}",
                  int(datetime.now().timestamp() * 1000)))
            con.commit()
        finally:
            con.close()
        
        # Here you would integrate with your model deployment system
        # to actually perform the rollback
        self.logger.info(f"Rollback logged: {result.model_version} -> {previous_version}")
    
    def update_gate_config(self, gate_name: str, config: ThresholdConfig):
        """Update gate configuration"""
        con = connect()
        try:
            con.execute("""
                INSERT OR REPLACE INTO ci_threshold_configs
                (gate_name, config_json, updated_ts_ms, updated_by)
                VALUES (?, ?, ?, ?)
            """, (gate_name, json.dumps(config.to_dict()), 
                  int(datetime.now().timestamp() * 1000), "system"))
            con.commit()
        finally:
            con.close()
    
    def get_pipeline_history(
        self,
        model_name: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get pipeline execution history"""
        con = connect()
        try:
            query = "SELECT * FROM ci_pipeline_runs WHERE 1=1"
            params = []
            
            if model_name:
                query += " AND model_name = ?"
                params.append(model_name)
            
            query += " ORDER BY start_ts_ms DESC LIMIT ?"
            params.append(limit)
            
            rows = con.execute(query, params).fetchall()
            
            history = []
            for row in rows:
                history.append({
                    'pipeline_id': row[0],
                    'model_name': row[1],
                    'model_kind': row[2],
                    'model_version': row[3],
                    'overall_status': row[4],
                    'gate_results': json.loads(row[5]),
                    'start_ts_ms': row[6],
                    'end_ts_ms': row[7],
                    'total_execution_time_ms': row[8],
                    'triggered_by': row[9],
                    'environment': row[10],
                    'created_ts_ms': row[11]
                })
            
            return history
        finally:
            con.close()


# CLI interface
def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='CI Stress Testing System')
    parser.add_argument('--model-name', type=str, required=True, help='Model name')
    parser.add_argument('--model-kind', type=str, help='Model kind')
    parser.add_argument('--model-version', type=str, help='Model version')
    parser.add_argument('--gates', nargs='+', help='Gates to run')
    parser.add_argument('--triggered-by', type=str, default='manual', help='Trigger source')
    parser.add_argument('--environment', type=str, default='production', help='Environment')
    parser.add_argument('--history', action='store_true', help='Show pipeline history')
    parser.add_argument('--config', action='store_true', help='Show gate configurations')
    parser.add_argument('--update-config', action='store_true', help='Update gate configuration')
    
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO)
    ci_system = CIStressTesting()
    
    if args.history:
        history = ci_system.get_pipeline_history(args.model_name)
        print(f"\nCI Pipeline History for {args.model_name}:")
        print(f"{'Pipeline ID':<20} {'Status':<10} {'Version':<15} {'Duration(s)':<12} {'Triggered By'}")
        print("-" * 90)
        for run in history:
            duration = run['total_execution_time_ms'] / 1000
            print(f"{run['pipeline_id']:<20} {run['overall_status']:<10} {run['model_version']:<15} {duration:<12.1f} {run['triggered_by']}")
        return
    
    if args.config:
        print("\nGate Configurations:")
        for gate_name, config in ci_system.default_thresholds.items():
            print(f"\n{gate_name}:")
            print(f"  Enabled: {config.enabled}")
            print(f"  Pass Threshold: {config.pass_threshold}")
            print(f"  Fail Threshold: {config.fail_threshold}")
            print(f"  Required Scenarios: {config.required_scenarios}")
        return
    
    if args.update_config:
        # Example: update configuration
        config = ci_system.default_thresholds['pre_promotion_stress_test']
        config.pass_threshold = 0.85
        ci_system.update_gate_config('pre_promotion_stress_test', config)
        print("Updated pre_promotion_stress_test gate configuration")
        return
    
    # Run CI pipeline
    result = ci_system.run_ci_pipeline(
        model_name=args.model_name,
        model_kind=args.model_kind,
        model_version=args.model_version,
        triggered_by=args.triggered_by,
        environment=args.environment,
        gates=args.gates
    )
    
    # Print results
    print(f"\nCI Pipeline Results:")
    print(f"Pipeline ID: {result.pipeline_id}")
    print(f"Overall Status: {result.overall_status.value}")
    print(f"Execution Time: {result.total_execution_time_ms / 1000:.1f}s")
    print(f"Model: {result.model_name} ({result.model_kind or 'N/A'})")
    print(f"Version: {result.model_version}")
    
    print(f"\nGate Results:")
    for gate in result.gate_results:
        score_info = f" (Score: {gate.score:.3f}, Threshold: {gate.threshold:.3f})" if gate.score is not None else ""
        print(f"  {gate.gate_name}: {gate.status.value}{score_info}")
    
    # Exit with appropriate code
    exit_code = 0 if result.overall_status == GateStatus.PASSED else 1
    exit(exit_code)


if __name__ == "__main__":
    main()
