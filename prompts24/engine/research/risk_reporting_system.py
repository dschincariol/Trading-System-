# risk_reporting_system.py
"""
Structured Risk Reporting System for Adversarial Stress Testing

This module provides comprehensive, automated risk reporting capabilities for the
adversarial stress testing system. It generates structured reports suitable for:
- Model governance committees
- Risk management teams
- Regulatory compliance
- Executive summaries
- Technical deep-dives

Report Types:
- Executive Summary Reports
- Technical Analysis Reports
- Regulatory Compliance Reports
- Model Risk Reports
- Incident Response Reports
"""

import os
import json
import logging
from dataclasses import dataclass, asdict
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
from enum import Enum
import pandas as pd

from engine.storage import connect
from engine.research.adversarial_scenario_generator import ScenarioType
from engine.research.model_fragility_analyzer import FragilityDimension, FailureMode


class ReportType(Enum):
    EXECUTIVE_SUMMARY = "executive_summary"
    TECHNICAL_ANALYSIS = "technical_analysis"
    REGULATORY_COMPLIANCE = "regulatory_compliance"
    MODEL_RISK = "model_risk"
    INCIDENT_RESPONSE = "incident_response"
    TREND_ANALYSIS = "trend_analysis"


class RiskLevel(Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    MINIMAL = "minimal"


@dataclass
class ReportSection:
    """Individual section of a risk report"""
    title: str
    content: str
    risk_level: RiskLevel
    metrics: Dict[str, Any]
    recommendations: List[str]
    
    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result['risk_level'] = self.risk_level.value
        return result


@dataclass
class RiskReport:
    """Complete risk report structure"""
    report_id: str
    report_type: ReportType
    model_name: str
    model_kind: Optional[str]
    generated_ts_ms: int
    period_start_ts_ms: int
    period_end_ts_ms: int
    overall_risk_level: RiskLevel
    executive_summary: str
    sections: List[ReportSection]
    key_findings: List[str]
    immediate_actions: List[str]
    appendices: Dict[str, Any]
    
    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result['report_type'] = self.report_type.value
        result['overall_risk_level'] = self.overall_risk_level.value
        result['sections'] = [section.to_dict() for section in self.sections]
        return result


class RiskReportingSystem:
    """Comprehensive risk reporting system"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self._ensure_tables()
        self.report_templates = self._initialize_templates()
    
    def _ensure_tables(self):
        """Create database tables for risk reporting"""
        schema = """
        CREATE TABLE IF NOT EXISTS risk_reports (
            report_id TEXT PRIMARY KEY,
            report_type TEXT NOT NULL,
            model_name TEXT NOT NULL,
            model_kind TEXT,
            generated_ts_ms INTEGER NOT NULL,
            period_start_ts_ms INTEGER NOT NULL,
            period_end_ts_ms INTEGER NOT NULL,
            overall_risk_level TEXT NOT NULL,
            report_json TEXT NOT NULL,
            status TEXT DEFAULT 'generated'
        );
        
        CREATE TABLE IF NOT EXISTS report_templates (
            template_name TEXT PRIMARY KEY,
            template_type TEXT NOT NULL,
            template_json TEXT NOT NULL,
            updated_ts_ms INTEGER NOT NULL
        );
        
        CREATE TABLE IF NOT EXISTS report_schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_type TEXT NOT NULL,
            model_name TEXT NOT NULL,
            schedule_expression TEXT NOT NULL,  -- cron-like expression
            recipients TEXT,  -- JSON array of emails
            enabled BOOLEAN DEFAULT 1,
            last_run_ts_ms INTEGER,
            next_run_ts_ms INTEGER,
            created_ts_ms INTEGER NOT NULL
        );
        
        CREATE INDEX IF NOT EXISTS idx_risk_reports_model ON risk_reports(model_name, generated_ts_ms);
        CREATE INDEX IF NOT EXISTS idx_risk_reports_type ON risk_reports(report_type);
        CREATE INDEX IF NOT EXISTS idx_report_schedule_next ON report_schedule(next_run_ts_ms);
        """
        
        con = connect()
        try:
            con.executescript(schema)
            con.commit()
        finally:
            con.close()
    
    def _initialize_templates(self) -> Dict[ReportType, Dict[str, Any]]:
        """Initialize report templates"""
        return {
            ReportType.EXECUTIVE_SUMMARY: {
                'title': 'Executive Risk Summary',
                'audience': 'Senior Management',
                'sections': [
                    'overall_risk_assessment',
                    'key_metrics',
                    'critical_findings',
                    'immediate_actions',
                    'resource_requirements'
                ],
                'format': 'markdown',
                'length_guide': '1-2 pages'
            },
            ReportType.TECHNICAL_ANALYSIS: {
                'title': 'Technical Risk Analysis',
                'audience': 'Model Development Team',
                'sections': [
                    'methodology',
                    'scenario_analysis',
                    'fragility_dimensions',
                    'failure_mode_analysis',
                    'performance_degradation',
                    'remediation_recommendations'
                ],
                'format': 'markdown',
                'length_guide': '5-10 pages'
            },
            ReportType.REGULATORY_COMPLIANCE: {
                'title': 'Model Risk Regulatory Report',
                'audience': 'Compliance Officers',
                'sections': [
                    'governance_overview',
                    'risk_framework_compliance',
                    'stress_test_coverage',
                    'model_validation_status',
                    'risk_mitigation_measures',
                    'audit_trail'
                ],
                'format': 'markdown',
                'length_guide': '3-5 pages'
            },
            ReportType.MODEL_RISK: {
                'title': 'Model Risk Assessment',
                'audience': 'Risk Management',
                'sections': [
                    'risk_identification',
                    'risk_measurement',
                    'risk_monitoring',
                    'risk_mitigation',
                    'residual_risk_assessment'
                ],
                'format': 'markdown',
                'length_guide': '4-6 pages'
            },
            ReportType.INCIDENT_RESPONSE: {
                'title': 'Stress Test Incident Report',
                'audience': 'Incident Response Team',
                'sections': [
                    'incident_summary',
                    'timeline',
                    'impact_assessment',
                    'root_cause_analysis',
                    'corrective_actions',
                    'prevention_measures'
                ],
                'format': 'markdown',
                'length_guide': '2-4 pages'
            },
            ReportType.TREND_ANALYSIS: {
                'title': 'Risk Trend Analysis',
                'audience': 'Model Governance',
                'sections': [
                    'trend_overview',
                    'historical_comparison',
                    'risk_trajectory',
                    'early_warning_indicators',
                    'forecast_analysis'
                ],
                'format': 'markdown',
                'length_guide': '3-5 pages'
            }
        }
    
    def generate_executive_summary_report(
        self,
        model_name: str,
        model_kind: Optional[str] = None,
        period_days: int = 30
    ) -> RiskReport:
        """Generate executive summary risk report"""
        
        self.logger.info(f"Generating executive summary for {model_name}")
        
        report_id = f"exec_summary_{model_name}_{int(datetime.now().timestamp() * 1000)}"
        now_ms = int(datetime.now().timestamp() * 1000)
        start_ms = now_ms - period_days * 86400 * 1000
        
        # Gather data
        recent_stress_tests = self._get_recent_stress_tests(model_name, model_kind, start_ms, now_ms)
        fragility_profile = self._get_latest_fragility_profile(model_name, model_kind)
        early_warnings = self._get_recent_warnings(model_name, start_ms, now_ms)
        
        # Analyze overall risk
        overall_risk = self._assess_overall_risk(recent_stress_tests, fragility_profile, early_warnings)
        
        # Generate sections
        sections = [
            self._create_overall_risk_section(overall_risk, recent_stress_tests),
            self._create_key_metrics_section(recent_stress_tests, fragility_profile),
            self._create_critical_findings_section(early_warnings, recent_stress_tests),
            self._create_immediate_actions_section(overall_risk, fragility_profile),
            self._create_resource_requirements_section(overall_risk)
        ]
        
        # Generate executive summary
        exec_summary = self._generate_executive_summary(overall_risk, sections)
        
        # Extract key findings and actions
        key_findings = self._extract_key_findings(sections)
        immediate_actions = self._extract_immediate_actions(sections)
        
        report = RiskReport(
            report_id=report_id,
            report_type=ReportType.EXECUTIVE_SUMMARY,
            model_name=model_name,
            model_kind=model_kind,
            generated_ts_ms=now_ms,
            period_start_ts_ms=start_ms,
            period_end_ts_ms=now_ms,
            overall_risk_level=overall_risk,
            executive_summary=exec_summary,
            sections=sections,
            key_findings=key_findings,
            immediate_actions=immediate_actions,
            appendices=self._generate_executive_appendices(recent_stress_tests, fragility_profile)
        )
        
        # Store report
        self._store_report(report)
        
        return report
    
    def generate_technical_analysis_report(
        self,
        model_name: str,
        model_kind: Optional[str] = None,
        period_days: int = 30
    ) -> RiskReport:
        """Generate detailed technical analysis report"""
        
        self.logger.info(f"Generating technical analysis for {model_name}")
        
        report_id = f"tech_analysis_{model_name}_{int(datetime.now().timestamp() * 1000)}"
        now_ms = int(datetime.now().timestamp() * 1000)
        start_ms = now_ms - period_days * 86400 * 1000
        
        # Gather detailed data
        recent_stress_tests = self._get_recent_stress_tests(model_name, model_kind, start_ms, now_ms)
        fragility_profile = self._get_latest_fragility_profile(model_name, model_kind)
        failure_clusters = self._get_failure_clusters(model_name)
        historical_trends = self._get_historical_trends(model_name, start_ms, now_ms)
        
        # Assess technical risk
        overall_risk = self._assess_technical_risk(recent_stress_tests, fragility_profile, failure_clusters)
        
        # Generate technical sections
        sections = [
            self._create_methodology_section(),
            self._create_scenario_analysis_section(recent_stress_tests),
            self._create_fragility_dimensions_section(fragility_profile),
            self._create_failure_mode_analysis_section(failure_clusters),
            self._create_performance_degradation_section(recent_stress_tests),
            self._create_technical_remediation_section(fragility_profile, failure_clusters)
        ]
        
        # Generate executive summary for technical audience
        exec_summary = self._generate_technical_executive_summary(overall_risk, sections)
        
        key_findings = self._extract_technical_key_findings(sections)
        immediate_actions = self._extract_technical_immediate_actions(sections)
        
        report = RiskReport(
            report_id=report_id,
            report_type=ReportType.TECHNICAL_ANALYSIS,
            model_name=model_name,
            model_kind=model_kind,
            generated_ts_ms=now_ms,
            period_start_ts_ms=start_ms,
            period_end_ts_ms=now_ms,
            overall_risk_level=overall_risk,
            executive_summary=exec_summary,
            sections=sections,
            key_findings=key_findings,
            immediate_actions=immediate_actions,
            appendices=self._generate_technical_appendices(
                recent_stress_tests, fragility_profile, failure_clusters, historical_trends
            )
        )
        
        self._store_report(report)
        return report
    
    def generate_regulatory_compliance_report(
        self,
        model_name: str,
        model_kind: Optional[str] = None,
        compliance_framework: str = "SR11_7"
    ) -> RiskReport:
        """Generate regulatory compliance report"""
        
        self.logger.info(f"Generating regulatory compliance report for {model_name}")
        
        report_id = f"reg_compliance_{model_name}_{int(datetime.now().timestamp() * 1000)}"
        now_ms = int(datetime.now().timestamp() * 1000)
        start_ms = now_ms - 90 * 86400 * 1000  # 90-day lookback for compliance
        
        # Gather compliance data
        stress_test_coverage = self._assess_stress_test_coverage(model_name, start_ms, now_ms)
        governance_status = self._assess_governance_compliance(model_name)
        validation_status = self._assess_model_validation(model_name)
        risk_mitigation = self._assess_risk_mitigation_measures(model_name)
        audit_trail = self._generate_audit_trail(model_name, start_ms, now_ms)
        
        # Assess compliance risk
        overall_risk = self._assess_compliance_risk(
            stress_test_coverage, governance_status, validation_status, risk_mitigation
        )
        
        # Generate compliance sections
        sections = [
            self._create_governance_overview_section(governance_status),
            self._create_risk_framework_section(compliance_framework),
            self._create_stress_test_coverage_section(stress_test_coverage),
            self._create_model_validation_section(validation_status),
            self._create_risk_mitigation_section(risk_mitigation),
            self._create_audit_trail_section(audit_trail)
        ]
        
        exec_summary = self._generate_compliance_executive_summary(overall_risk, compliance_framework)
        
        key_findings = self._extract_compliance_key_findings(sections)
        immediate_actions = self._extract_compliance_immediate_actions(sections)
        
        report = RiskReport(
            report_id=report_id,
            report_type=ReportType.REGULATORY_COMPLIANCE,
            model_name=model_name,
            model_kind=model_kind,
            generated_ts_ms=now_ms,
            period_start_ts_ms=start_ms,
            period_end_ts_ms=now_ms,
            overall_risk_level=overall_risk,
            executive_summary=exec_summary,
            sections=sections,
            key_findings=key_findings,
            immediate_actions=immediate_actions,
            appendices=self._generate_compliance_appendices(
                stress_test_coverage, governance_status, validation_status, audit_trail
            )
        )
        
        self._store_report(report)
        return report
    
    def _get_recent_stress_tests(
        self,
        model_name: str,
        model_kind: Optional[str],
        start_ms: int,
        end_ms: int
    ) -> List[Dict[str, Any]]:
        """Get recent stress test results"""
        con = connect()
        try:
            query = """
                SELECT s.test_batch_id, s.summary_json, s.created_ts_ms
                FROM stress_test_summary s
                JOIN stress_test_gates g ON s.test_batch_id = g.test_batch_id
                WHERE g.model_name = ? AND s.created_ts_ms BETWEEN ? AND ?
            """
            params = [model_name, start_ms, end_ms]
            
            if model_kind:
                query += " AND g.model_kind = ?"
                params.append(model_kind)
            
            query += " ORDER BY s.created_ts_ms DESC"
            
            rows = con.execute(query, params).fetchall()
            
            results = []
            for row in rows:
                summary = json.loads(row[1])
                results.append({
                    'test_batch_id': row[0],
                    'summary': summary,
                    'created_ts_ms': row[2]
                })
            
            return results
        finally:
            con.close()
    
    def _get_latest_fragility_profile(
        self,
        model_name: str,
        model_kind: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Get latest fragility profile"""
        con = connect()
        try:
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
            
            if row:
                return json.loads(row[0])
            return None
        finally:
            con.close()
    
    def _get_recent_warnings(
        self,
        model_name: str,
        start_ms: int,
        end_ms: int
    ) -> List[Dict[str, Any]]:
        """Get recent early warnings"""
        con = connect()
        try:
            rows = con.execute("""
                SELECT warning_type, severity, message, metrics_json, created_ts_ms
                FROM early_warnings
                WHERE model_name = ? AND created_ts_ms BETWEEN ? AND ?
                ORDER BY created_ts_ms DESC
            """, (model_name, start_ms, end_ms)).fetchall()
            
            warnings = []
            for row in rows:
                warnings.append({
                    'warning_type': row[0],
                    'severity': row[1],
                    'message': row[2],
                    'metrics': json.loads(row[3]) if row[3] else {},
                    'created_ts_ms': row[4]
                })
            
            return warnings
        finally:
            con.close()
    
    def _assess_overall_risk(
        self,
        stress_tests: List[Dict[str, Any]],
        fragility_profile: Optional[Dict[str, Any]],
        warnings: List[Dict[str, Any]]
    ) -> RiskLevel:
        """Assess overall risk level for executive summary"""
        
        risk_score = 0.0
        
        # Stress test performance
        if stress_tests:
            avg_pass_rate = sum(s['summary'].get('pass_rate', 0) for s in stress_tests) / len(stress_tests)
            stress_risk = 1.0 - avg_pass_rate
            risk_score += 0.4 * stress_risk
        
        # Fragility score
        if fragility_profile:
            fragility = fragility_profile.get('overall_fragility', 0)
            risk_score += 0.4 * fragility
        
        # Early warnings
        critical_warnings = sum(1 for w in warnings if w['severity'] == 'critical')
        high_warnings = sum(1 for w in warnings if w['severity'] == 'high')
        warning_risk = min(1.0, (critical_warnings * 0.3 + high_warnings * 0.15))
        risk_score += 0.2 * warning_risk
        
        # Convert score to risk level
        if risk_score >= 0.8:
            return RiskLevel.CRITICAL
        elif risk_score >= 0.6:
            return RiskLevel.HIGH
        elif risk_score >= 0.4:
            return RiskLevel.MEDIUM
        elif risk_score >= 0.2:
            return RiskLevel.LOW
        else:
            return RiskLevel.MINIMAL
    
    def _create_overall_risk_section(
        self,
        risk_level: RiskLevel,
        stress_tests: List[Dict[str, Any]]
    ) -> ReportSection:
        """Create overall risk assessment section"""
        
        content = f"""
## Overall Risk Assessment

**Current Risk Level: {risk_level.value.upper()}**

### Risk Summary
"""
        
        if stress_tests:
            latest_test = stress_tests[0]['summary']
            content += f"""
- **Latest Stress Test Pass Rate:** {latest_test.get('pass_rate', 0):.1%}
- **Average Fragility Score:** {latest_test.get('avg_fragility_score', 0):.3f}
- **Scenarios Tested:** {latest_test.get('scenario_count', 0)}
- **Worst Failure Mode:** {latest_test.get('worst_failure_mode', 'None')}
"""
        
        # Risk trend analysis
        if len(stress_tests) >= 2:
            recent_pass_rates = [s['summary'].get('pass_rate', 0) for s in stress_tests[:5]]
            trend = "improving" if recent_pass_rates[0] > recent_pass_rates[-1] else "declining"
            content += f"\n- **Risk Trend:** {trend.title()}\n"
        
        # Risk level interpretation
        risk_descriptions = {
            RiskLevel.CRITICAL: "Immediate action required. Model poses significant risk to operations.",
            RiskLevel.HIGH: "Elevated risk requiring prompt attention and mitigation measures.",
            RiskLevel.MEDIUM: "Moderate risk with acceptable controls, but monitoring required.",
            RiskLevel.LOW: "Low risk within acceptable parameters.",
            RiskLevel.MINIMAL: "Minimal risk, well within acceptable thresholds."
        }
        
        content += f"\n### Risk Interpretation\n{risk_descriptions[risk_level]}\n"
        
        return ReportSection(
            title="Overall Risk Assessment",
            content=content,
            risk_level=risk_level,
            metrics={
                'risk_level': risk_level.value,
                'latest_pass_rate': stress_tests[0]['summary'].get('pass_rate', 0) if stress_tests else 0,
                'avg_fragility': stress_tests[0]['summary'].get('avg_fragility_score', 0) if stress_tests else 0
            },
            recommendations=self._get_risk_level_recommendations(risk_level)
        )
    
    def _create_key_metrics_section(
        self,
        stress_tests: List[Dict[str, Any]],
        fragility_profile: Optional[Dict[str, Any]]
    ) -> ReportSection:
        """Create key metrics section"""
        
        content = """
## Key Performance Metrics

### Stress Test Performance
"""
        
        if stress_tests:
            latest = stress_tests[0]['summary']
            content += f"""
| Metric | Value | Status |
|--------|-------|---------|
| Pass Rate | {latest.get('pass_rate', 0):.1%} | {'✅' if latest.get('pass_rate', 0) >= 0.7 else '❌'} |
| Avg Fragility | {latest.get('avg_fragility_score', 0):.3f} | {'✅' if latest.get('avg_fragility_score', 0) <= 0.3 else '❌'} |
| Scenarios Passed | {latest.get('pass_count', 0)}/{latest.get('scenario_count', 0)} | {'✅' if latest.get('pass_count', 0) >= latest.get('scenario_count', 0) * 0.7 else '❌'} |
"""
        
        if fragility_profile:
            content += "\n### Fragility Dimensions\n"
            dimensions = fragility_profile.get('dimension_scores', {})
            for dim, score in dimensions.items():
                status = '✅' if score <= 0.4 else '⚠️' if score <= 0.7 else '❌'
                content += f"- **{dim.replace('_', ' ').title()}:** {score:.3f} {status}\n"
        
        return ReportSection(
            title="Key Metrics",
            content=content,
            risk_level=RiskLevel.MEDIUM,  # Determined by individual metrics
            metrics={
                'pass_rate': stress_tests[0]['summary'].get('pass_rate', 0) if stress_tests else 0,
                'fragility_score': fragility_profile.get('overall_fragility', 0) if fragility_profile else 0
            },
            recommendations=["Continue monitoring key metrics", "Investigate metric degradation"]
        )
    
    def _create_critical_findings_section(
        self,
        warnings: List[Dict[str, Any]],
        stress_tests: List[Dict[str, Any]]
    ) -> ReportSection:
        """Create critical findings section"""
        
        content = "## Critical Findings\n\n"
        
        critical_items = []
        
        # Add critical warnings
        for warning in warnings:
            if warning['severity'] in ['critical', 'high']:
                critical_items.append(f"⚠️ **{warning['warning_type'].replace('_', ' ').title()}:** {warning['message']}")
        
        # Add failed stress test scenarios
        if stress_tests:
            latest = stress_tests[0]['summary']
            if latest.get('pass_rate', 1.0) < 0.7:
                critical_items.append(f"❌ **Stress Test Failure:** Pass rate of {latest.get('pass_rate', 0):.1%} below threshold")
            
            worst_failure = latest.get('worst_failure_mode')
            if worst_failure:
                critical_items.append(f"🔍 **Primary Failure Mode:** {worst_failure.replace('_', ' ').title()}")
        
        if critical_items:
            content += "\n".join(critical_items[:5])  # Top 5 critical items
        else:
            content += "✅ No critical findings identified in the current period."
        
        # Determine risk level based on findings
        risk_level = RiskLevel.CRITICAL if any('critical' in w['severity'] for w in warnings) else \
                    RiskLevel.HIGH if any('high' in w['severity'] for w in warnings) else \
                    RiskLevel.MEDIUM
        
        return ReportSection(
            title="Critical Findings",
            content=content,
            risk_level=risk_level,
            metrics={
                'critical_warnings': sum(1 for w in warnings if w['severity'] == 'critical'),
                'high_warnings': sum(1 for w in warnings if w['severity'] == 'high'),
                'failed_scenarios': stress_tests[0]['summary'].get('fail_count', 0) if stress_tests else 0
            },
            recommendations=["Address critical warnings immediately", "Review failed scenario patterns"]
        )
    
    def _create_immediate_actions_section(
        self,
        risk_level: RiskLevel,
        fragility_profile: Optional[Dict[str, Any]]
    ) -> ReportSection:
        """Create immediate actions section"""
        
        content = "## Immediate Actions Required\n\n"
        
        actions = []
        
        if risk_level in [RiskLevel.CRITICAL, RiskLevel.HIGH]:
            actions.extend([
            "🚨 **Immediate:** Review and pause model deployment if necessary",
            "📋 **Within 24 hours:** Conduct emergency model review meeting",
            "🔧 **Within 48 hours:** Implement temporary risk controls"
        ])
        
        if fragility_profile:
            remediation = fragility_profile.get('remediation_priorities', [])
            for i, priority in enumerate(remediation[:3]):  # Top 3 priorities
                actions.append(f"🎯 **Priority {i+1}:** {priority.get('dimension', 'Unknown').replace('_', ' ').title()}")
        
        if not actions:
            actions.append("✅ No immediate actions required - continue normal monitoring")
        
        content += "\n".join(actions)
        
        return ReportSection(
            title="Immediate Actions",
            content=content,
            risk_level=risk_level,
            metrics={'action_count': len(actions)},
            recommendations=["Schedule follow-up review", "Track action completion"]
        )
    
    def _create_resource_requirements_section(self, risk_level: RiskLevel) -> ReportSection:
        """Create resource requirements section"""
        
        content = "## Resource Requirements\n\n"
        
        resource_map = {
            RiskLevel.CRITICAL: {
                'personnel': ['Senior Model Developer', 'Risk Manager', 'Quant Analyst', 'DevOps Engineer'],
                'time': '2-3 weeks full-time equivalent',
                'budget': 'High priority allocation',
                'external_support': 'May require external consultants'
            },
            RiskLevel.HIGH: {
                'personnel': ['Model Developer', 'Risk Analyst', 'QA Engineer'],
                'time': '1-2 weeks',
                'budget': 'Medium priority',
                'external_support': 'Internal resources likely sufficient'
            },
            RiskLevel.MEDIUM: {
                'personnel': ['Model Developer', 'Risk Analyst'],
                'time': '1 week',
                'budget': 'Standard allocation',
                'external_support': 'Not required'
            },
            RiskLevel.LOW: {
                'personnel': ['Model Developer'],
                'time': '2-3 days',
                'budget': 'Minimal',
                'external_support': 'Not required'
            },
            RiskLevel.MINIMAL: {
                'personnel': ['Monitoring staff'],
                'time': 'Ongoing monitoring',
                'budget': 'Standard operations',
                'external_support': 'Not required'
            }
        }
        
        resources = resource_map[risk_level]
        
        content += f"""
### Personnel Required
{', '.join(resources['personnel'])}

### Time Commitment
{resources['time']}

### Budget Priority
{resources['budget']}

### External Support
{resources['external_support']}
"""
        
        return ReportSection(
            title="Resource Requirements",
            content=content,
            risk_level=risk_level,
            metrics={'personnel_count': len(resources['personnel'])},
            recommendations=["Allocate resources promptly", "Monitor resource utilization"]
        )
    
    def _generate_executive_summary(
        self,
        risk_level: RiskLevel,
        sections: List[ReportSection]
    ) -> str:
        """Generate executive summary"""
        
        summary = f"""
# Executive Risk Summary

**Risk Level:** {risk_level.value.upper()}

## Overview
The model has undergone comprehensive adversarial stress testing with {'critical' if risk_level in [RiskLevel.CRITICAL, RiskLevel.HIGH] else 'acceptable'} results.
"""
        
        # Add key highlights from sections
        for section in sections:
            if section.title == "Overall Risk Assessment":
                summary += f"\n{section.content.split('### Risk Interpretation')[0]}\n"
                break
        
        summary += f"""
## Key Takeaways
- Current model risk is classified as {risk_level.value}
- {'Immediate action required' if risk_level in [RiskLevel.CRITICAL, RiskLevel.HIGH] else 'Continue monitoring with standard controls'}
- Stress test coverage meets {'minimum' if risk_level != RiskLevel.CRITICAL else 'insufficient'} requirements
- {'Critical' if risk_level == RiskLevel.CRITICAL else 'Elevated' if risk_level == RiskLevel.HIGH else 'Standard'} attention from model governance team required

## Next Steps
{sections[3].content.split('##')[1] if len(sections) > 3 else 'Continue standard monitoring procedures.'}
"""
        
        return summary
    
    def _extract_key_findings(self, sections: List[ReportSection]) -> List[str]:
        """Extract key findings from sections"""
        findings = []
        
        for section in sections:
            if section.title == "Critical Findings":
                # Extract bullet points from critical findings
                lines = section.content.split('\n')
                for line in lines:
                    if line.startswith('⚠️') or line.startswith('❌') or line.startswith('🔍'):
                        findings.append(line.strip())
        
        return findings[:5]  # Top 5 findings
    
    def _extract_immediate_actions(self, sections: List[ReportSection]) -> List[str]:
        """Extract immediate actions from sections"""
        actions = []
        
        for section in sections:
            if section.title == "Immediate Actions":
                # Extract bullet points from immediate actions
                lines = section.content.split('\n')
                for line in lines:
                    if line.startswith('🚨') or line.startswith('📋') or line.startswith('🔧') or line.startswith('🎯'):
                        actions.append(line.strip())
        
        return actions[:5]  # Top 5 actions
    
    def _generate_executive_appendices(
        self,
        stress_tests: List[Dict[str, Any]],
        fragility_profile: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Generate appendices for executive report"""
        
        appendices = {}
        
        # Stress test summary table
        if stress_tests:
            appendices['stress_test_summary'] = {
                'latest_results': stress_tests[0]['summary'],
                'trend_data': [{'date': s['created_ts_ms'], 'pass_rate': s['summary'].get('pass_rate', 0)} for s in stress_tests[:5]]
            }
        
        # Fragility breakdown
        if fragility_profile:
            appendices['fragility_breakdown'] = {
                'overall_score': fragility_profile.get('overall_fragility', 0),
                'dimensions': fragility_profile.get('dimension_scores', {}),
                'failure_modes': fragility_profile.get('failure_modes', [])
            }
        
        return appendices
    
    def _get_risk_level_recommendations(self, risk_level: RiskLevel) -> List[str]:
        """Get recommendations based on risk level"""
        recommendations_map = {
            RiskLevel.CRITICAL: [
                "Immediately review model deployment status",
                "Conduct emergency model governance meeting",
                "Implement temporary risk controls",
                "Consider model rollback if necessary"
            ],
            RiskLevel.HIGH: [
                "Schedule urgent model review",
                "Enhanced monitoring procedures",
                "Develop remediation plan",
                "Increase testing frequency"
            ],
            RiskLevel.MEDIUM: [
                "Continue standard monitoring",
                "Review in next governance cycle",
                "Consider minor adjustments",
                "Maintain current controls"
            ],
            RiskLevel.LOW: [
                "Continue normal operations",
                "Standard monitoring procedures",
                "Periodic review scheduled"
            ],
            RiskLevel.MINIMAL: [
                "Continue normal operations",
                "Standard monitoring sufficient"
            ]
        }
        
        return recommendations_map.get(risk_level, [])
    
    def _store_report(self, report: RiskReport):
        """Store report in database"""
        con = connect()
        try:
            con.execute("""
                INSERT INTO risk_reports
                (report_id, report_type, model_name, model_kind, generated_ts_ms,
                 period_start_ts_ms, period_end_ts_ms, overall_risk_level, report_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (report.report_id, report.report_type.value, report.model_name, report.model_kind,
                  report.generated_ts_ms, report.period_start_ts_ms, report.period_end_ts_ms,
                  report.overall_risk_level.value, json.dumps(report.to_dict())))
            con.commit()
        finally:
            con.close()
    
    def format_report_for_output(self, report: RiskReport) -> str:
        """Format report for output (markdown)"""
        
        output = f"""
# {self.report_templates[report.report_type]['title']}

**Model:** {report.model_name} ({report.model_kind or 'N/A'})
**Generated:** {datetime.fromtimestamp(report.generated_ts_ms/1000).strftime('%Y-%m-%d %H:%M:%S')}
**Period:** {datetime.fromtimestamp(report.period_start_ts_ms/1000).strftime('%Y-%m-%d')} to {datetime.fromtimestamp(report.period_end_ts_ms/1000).strftime('%Y-%m-%d')}
**Risk Level:** {report.overall_risk_level.value.upper()}

---

{report.executive_summary}

---

## Key Findings
"""
        
        for finding in report.key_findings:
            output += f"\n{finding}"
        
        output += "\n\n## Immediate Actions\n"
        for action in report.immediate_actions:
            output += f"\n{action}"
        
        output += "\n\n---\n\n"
        
        for section in report.sections:
            output += f"{section.content}\n\n---\n\n"
        
        return output
    
    def get_report_history(
        self,
        model_name: Optional[str] = None,
        report_type: Optional[ReportType] = None,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Get report history"""
        con = connect()
        try:
            query = "SELECT report_id, report_type, model_name, generated_ts_ms, overall_risk_level FROM risk_reports WHERE 1=1"
            params = []
            
            if model_name:
                query += " AND model_name = ?"
                params.append(model_name)
            
            if report_type:
                query += " AND report_type = ?"
                params.append(report_type.value)
            
            query += " ORDER BY generated_ts_ms DESC LIMIT ?"
            params.append(limit)
            
            rows = con.execute(query, params).fetchall()
            
            history = []
            for row in rows:
                history.append({
                    'report_id': row[0],
                    'report_type': row[1],
                    'model_name': row[2],
                    'generated_ts_ms': row[3],
                    'overall_risk_level': row[4],
                    'generated_date': datetime.fromtimestamp(row[3]/1000).strftime('%Y-%m-%d %H:%M')
                })
            
            return history
        finally:
            con.close()


# CLI interface
def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Risk Reporting System')
    parser.add_argument('--model-name', type=str, required=True, help='Model name')
    parser.add_argument('--model-kind', type=str, help='Model kind')
    parser.add_argument('--report-type', choices=['executive_summary', 'technical_analysis', 'regulatory_compliance'],
                       default='executive_summary', help='Report type to generate')
    parser.add_argument('--period-days', type=int, default=30, help='Analysis period in days')
    parser.add_argument('--output', type=str, help='Output file path')
    parser.add_argument('--history', action='store_true', help='Show report history')
    parser.add_argument('--format', choices=['markdown', 'json'], default='markdown', help='Output format')
    
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO)
    reporter = RiskReportingSystem()
    
    if args.history:
        history = reporter.get_report_history(args.model_name, ReportType(args.report_type))
        print(f"\nReport History for {args.model_name}:")
        print(f"{'ID':<20} {'Type':<20} {'Risk Level':<15} {'Generated'}")
        print("-" * 80)
        for report in history:
            print(f"{report['report_id']:<20} {report['report_type']:<20} {report['overall_risk_level']:<15} {report['generated_date']}")
        return
    
    # Generate report
    if args.report_type == 'executive_summary':
        report = reporter.generate_executive_summary_report(args.model_name, args.model_kind, args.period_days)
    elif args.report_type == 'technical_analysis':
        report = reporter.generate_technical_analysis_report(args.model_name, args.model_kind, args.period_days)
    elif args.report_type == 'regulatory_compliance':
        report = reporter.generate_regulatory_compliance_report(args.model_name, args.model_kind)
    
    # Format and output
    if args.format == 'markdown':
        output = reporter.format_report_for_output(report)
    else:
        output = json.dumps(report.to_dict(), indent=2)
    
    if args.output:
        with open(args.output, 'w') as f:
            f.write(output)
        print(f"Report saved to {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    main()
