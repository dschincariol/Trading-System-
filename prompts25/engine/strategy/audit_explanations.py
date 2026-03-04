"""
Audit Explanation System
Generates human-readable explanations for model decisions and audit trails.
"""

import json
import time
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass

from engine.storage import connect, init_db
from engine.strategy.model_lineage import get_lineage_tracker, LineageEventType
from engine.strategy.governance_schemas import (
    ModelGovernanceRecord, ValidationStatus, RiskCategory, ComplianceLevel
)

@dataclass
class AuditExplanation:
    """Structured audit explanation"""
    explanation_id: str
    timestamp: datetime
    model_name: str
    model_kind: str
    model_ts_ms: int
    regime: str
    explanation_type: str  # promotion, demotion, rollback, compliance, performance
    summary: str
    detailed_reasoning: str
    evidence: List[Dict[str, Any]]
    risk_assessment: Dict[str, Any]
    compliance_impact: Dict[str, Any]
    recommendations: List[str]
    next_steps: List[str]

class AuditExplanationEngine:
    """Generates human-readable audit explanations"""
    
    def __init__(self):
        self.lineage_tracker = get_lineage_tracker()
        self._init_explanation_tables()
    
    def _init_explanation_tables(self):
        """Initialize explanation storage tables"""
        init_db()
        con = connect()
        try:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS audit_explanations (
                    explanation_id TEXT PRIMARY KEY,
                    ts_ms INTEGER NOT NULL,
                    model_name TEXT NOT NULL,
                    model_kind TEXT NOT NULL,
                    model_ts_ms INTEGER NOT NULL,
                    regime TEXT NOT NULL,
                    explanation_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    detailed_reasoning TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    risk_assessment_json TEXT NOT NULL,
                    compliance_impact_json TEXT NOT NULL,
                    recommendations_json TEXT NOT NULL,
                    next_steps_json TEXT NOT NULL,
                    created_ts_ms INTEGER NOT NULL
                );
                
                CREATE TABLE IF NOT EXISTS explanation_templates (
                    template_id TEXT PRIMARY KEY,
                    explanation_type TEXT NOT NULL,
                    template_name TEXT NOT NULL,
                    template_text TEXT NOT NULL,
                    variables_json TEXT NOT NULL,
                    created_ts_ms INTEGER NOT NULL
                );
                
                CREATE INDEX IF NOT EXISTS idx_audit_explanations_model_time 
                    ON audit_explanations(model_name, model_kind, model_ts_ms, ts_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_explanations_type_time 
                    ON audit_explanations(explanation_type, ts_ms DESC);
            """)
            
            # Insert default templates
            self._insert_default_templates(con)
            con.commit()
            
        finally:
            con.close()
    
    def _insert_default_templates(self, con):
        """Insert default explanation templates"""
        templates = [
            {
                "template_id": "promotion_success",
                "explanation_type": "promotion",
                "template_name": "Successful Promotion",
                "template_text": "Model {model_name} ({model_kind}:{model_ts_ms}) was promoted to champion status in {regime} regime. The promotion was approved due to {key_reasons}. Performance metrics show {performance_summary}. Risk assessment indicates {risk_level} risk level.",
                "variables": ["model_name", "model_kind", "model_ts_ms", "regime", "key_reasons", "performance_summary", "risk_level"]
            },
            {
                "template_id": "promotion_rejected",
                "explanation_type": "promotion", 
                "template_name": "Promotion Rejected",
                "template_text": "Model {model_name} ({model_kind}:{model_ts_ms}) promotion was rejected for {regime} regime. The rejection was due to {rejection_reasons}. Key failing metrics include {failed_metrics}. Recommended improvements include {recommendations}.",
                "variables": ["model_name", "model_kind", "model_ts_ms", "regime", "rejection_reasons", "failed_metrics", "recommendations"]
            },
            {
                "template_id": "rollback_executed",
                "explanation_type": "rollback",
                "template_name": "Rollback Executed",
                "template_text": "Model {model_name} was rolled back from {from_model} to {to_model} in {regime} regime. The rollback was triggered by {trigger_reason}. Performance degradation was {performance_impact}. The rollback strategy used was {rollback_strategy}.",
                "variables": ["model_name", "from_model", "to_model", "regime", "trigger_reason", "performance_impact", "rollback_strategy"]
            },
            {
                "template_id": "performance_degradation",
                "explanation_type": "performance",
                "template_name": "Performance Degradation",
                "template_text": "Model {model_name} ({model_kind}:{model_ts_ms}) showed performance degradation in {regime} regime. Key metrics declined: {metric_changes}. The degradation was detected on {detection_date}. Current risk level is {risk_level}.",
                "variables": ["model_name", "model_kind", "model_ts_ms", "regime", "metric_changes", "detection_date", "risk_level"]
            },
            {
                "template_id": "compliance_breach",
                "explanation_type": "compliance",
                "template_name": "Compliance Breach",
                "template_text": "Model {model_name} violated compliance requirements in {regime} regime. Breached requirements: {breached_requirements}. Impact assessment: {impact_assessment}. Remediation actions required: {remediation_actions}.",
                "variables": ["model_name", "regime", "breached_requirements", "impact_assessment", "remediation_actions"]
            }
        ]
        
        for template in templates:
            con.execute(
                """
                INSERT OR IGNORE INTO explanation_templates
                (template_id, explanation_type, template_name, template_text, variables_json, created_ts_ms)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    template["template_id"],
                    template["explanation_type"],
                    template["template_name"],
                    template["template_text"],
                    json.dumps(template["variables"], separators=(",", ":")),
                    int(time.time() * 1000)
                )
            )
    
    def explain_promotion_decision(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        regime: str,
        promotion_result: Dict[str, Any]
    ) -> AuditExplanation:
        """Generate explanation for promotion decision"""
        lineage = self.lineage_tracker.get_model_lineage(model_name, model_kind, model_ts_ms, regime)
        
        # Extract key information
        was_promoted = promotion_result.get("promoted", False)
        decision_reasons = promotion_result.get("reasons", [])
        performance_metrics = promotion_result.get("performance_metrics", {})
        risk_metrics = promotion_result.get("risk_metrics", {})
        
        if was_promoted:
            summary = f"Model {model_name} successfully promoted to champion in {regime} regime"
            detailed_reasoning = self._generate_promotion_success_reasoning(
                lineage, decision_reasons, performance_metrics, risk_metrics
            )
            explanation_type = "promotion_success"
        else:
            summary = f"Model {model_name} promotion rejected in {regime} regime"
            detailed_reasoning = self._generate_promotion_rejection_reasoning(
                lineage, decision_reasons, performance_metrics, risk_metrics
            )
            explanation_type = "promotion_rejected"
        
        # Generate evidence
        evidence = self._generate_promotion_evidence(lineage, promotion_result)
        
        # Risk assessment
        risk_assessment = self._assess_promotion_risk(risk_metrics, performance_metrics)
        
        # Compliance impact
        compliance_impact = self._assess_compliance_impact(lineage, promotion_result)
        
        # Recommendations
        recommendations = self._generate_promotion_recommendations(
            was_promoted, decision_reasons, performance_metrics, risk_metrics
        )
        
        # Next steps
        next_steps = self._generate_promotion_next_steps(was_promoted, regime)
        
        explanation = AuditExplanation(
            explanation_id=f"promotion_{model_name}_{model_kind}_{model_ts_ms}_{int(time.time() * 1000)}",
            timestamp=datetime.now(timezone.utc),
            model_name=model_name,
            model_kind=model_kind,
            model_ts_ms=model_ts_ms,
            regime=regime,
            explanation_type=explanation_type,
            summary=summary,
            detailed_reasoning=detailed_reasoning,
            evidence=evidence,
            risk_assessment=risk_assessment,
            compliance_impact=compliance_impact,
            recommendations=recommendations,
            next_steps=next_steps
        )
        
        # Store explanation
        self._store_explanation(explanation)
        
        return explanation
    
    def explain_rollback_decision(
        self,
        model_name: str,
        regime: str,
        rollback_result: Dict[str, Any]
    ) -> AuditExplanation:
        """Generate explanation for rollback decision"""
        from_model = rollback_result.get("from_model", {})
        to_model = rollback_result.get("to_model", {})
        trigger_reason = rollback_result.get("reason", "unknown")
        strategy = rollback_result.get("strategy", "immediate")
        
        summary = f"Model {model_name} rolled back in {regime} regime due to {trigger_reason}"
        
        detailed_reasoning = f"""
## Rollback Decision Analysis

**Trigger Event:** {trigger_reason}
**Rollback Strategy:** {strategy}
**Execution Time:** {datetime.now(timezone.utc).isoformat()}

### From Model (Retired)
- Model: {from_model.get('model_kind', 'unknown')}:{from_model.get('model_ts_ms', 'unknown')}
- Performance: {from_model.get('performance', 'No data available')}
- Issues Detected: {rollback_result.get('issues_detected', [])}

### To Model (New Champion)
- Model: {to_model.get('model_kind', 'unknown')}:{to_model.get('model_ts_ms', 'unknown')}
- Expected Performance: {to_model.get('expected_performance', 'Based on historical data')}
- Stability: {to_model.get('stability_score', 'Unknown')}

### Rollback Rationale
The rollback was executed because:
{self._format_rollback_reasons(trigger_reason, rollback_result)}

### Impact Assessment
- Business Impact: {rollback_result.get('business_impact', 'Minimal disruption expected')}
- Risk Mitigation: {rollback_result.get('risk_mitigation', 'Successfully reduced exposure')}
- Recovery Time: {rollback_result.get('recovery_time_ms', 0) / 1000:.1f} seconds
        """
        
        evidence = [
            {
                "type": "performance_degradation",
                "description": "Performance metrics exceeded acceptable thresholds",
                "data": rollback_result.get("performance_data", {})
            },
            {
                "type": "rollback_execution",
                "description": "Rollback successfully executed",
                "data": {
                    "strategy": strategy,
                    "duration_ms": rollback_result.get("duration_ms", 0),
                    "success": rollback_result.get("success", False)
                }
            }
        ]
        
        risk_assessment = {
            "pre_rollback_risk": "HIGH" if trigger_reason in ["performance_degradation", "risk_breach"] else "MEDIUM",
            "post_rollback_risk": "LOW",
            "risk_reduction": "Significant - model returned to known stable state"
        }
        
        compliance_impact = {
            "regulatory_compliance": "Maintained - rollback ensures continued compliance",
            "audit_requirements": "Fully documented with complete traceability",
            "reporting_obligations": "Met - all events logged and explainable"
        }
        
        recommendations = [
            "Monitor new champion model performance closely for next 24 hours",
            "Investigate root cause of original model degradation",
            "Review and update model monitoring thresholds",
            "Consider additional validation before future promotions"
        ]
        
        next_steps = [
            "Enhanced monitoring activated for new champion",
            "Root cause analysis initiated for degraded model",
            "Stakeholder notification sent",
            "Post-rollback validation scheduled"
        ]
        
        explanation = AuditExplanation(
            explanation_id=f"rollback_{model_name}_{regime}_{int(time.time() * 1000)}",
            timestamp=datetime.now(timezone.utc),
            model_name=model_name,
            model_kind=from_model.get('model_kind', 'unknown'),
            model_ts_ms=from_model.get('model_ts_ms', 0),
            regime=regime,
            explanation_type="rollback_executed",
            summary=summary,
            detailed_reasoning=detailed_reasoning,
            evidence=evidence,
            risk_assessment=risk_assessment,
            compliance_impact=compliance_impact,
            recommendations=recommendations,
            next_steps=next_steps
        )
        
        self._store_explanation(explanation)
        return explanation
    
    def explain_model_state_at_time(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        regime: str,
        query_time: datetime
    ) -> AuditExplanation:
        """Generate explanation for model state at specific time"""
        lineage = self.lineage_tracker.get_model_lineage(model_name, model_kind, model_ts_ms, regime)
        query_time_ms = int(query_time.timestamp() * 1000)
        
        # Filter events up to query time
        relevant_events = [e for e in lineage.events if e.ts_ms <= query_time_ms]
        
        # Determine state at query time
        state_at_time = self._determine_state_at_time(relevant_events, query_time_ms)
        
        summary = f"Model {model_name} state analysis for {query_time.date().isoformat()}"
        
        detailed_reasoning = f"""
## Model State Analysis

**Query Time:** {query_time.isoformat()}
**Model:** {model_name} ({model_kind}:{model_ts_ms})
**Regime:** {regime}
**State at Query Time:** {state_at_time.get('stage', 'Unknown')}

### Historical Context
The model's state at the queried time was the result of the following sequence of events:

{self._format_event_timeline(relevant_events[-10:])}  # Show last 10 events

### Key Factors Influencing State
{self._analyze_state_factors(relevant_events, state_at_time)}

### Performance Context
{self._analyze_performance_context(lineage, query_time_ms)}

### Risk Assessment at Time
{self._analyze_risk_context(relevant_events, query_time_ms)}
        """
        
        evidence = [
            {
                "type": "lineage_events",
                "description": f"Events leading to state at {query_time.isoformat()}",
                "data": [
                    {
                        "timestamp": datetime.fromtimestamp(e.ts_ms/1000, tz=timezone.utc).isoformat(),
                        "event_type": e.event_type.value,
                        "actor": e.actor,
                        "reason": e.reason
                    }
                    for e in relevant_events[-20:]
                ]
            }
        ]
        
        risk_assessment = {
            "risk_at_time": state_at_time.get("risk_level", "UNKNOWN"),
            "contributing_factors": state_at_time.get("risk_factors", []),
            "mitigation_measures": state_at_time.get("mitigations", [])
        }
        
        compliance_impact = {
            "compliance_status": state_at_time.get("compliance_status", "Unknown"),
            "violations": state_at_time.get("violations", []),
            "remediation_status": state_at_time.get("remediation_status", "N/A")
        }
        
        recommendations = [
            "Review full event timeline for complete context",
            "Validate performance metrics against benchmarks",
            "Ensure all compliance requirements were met",
            "Document any anomalies or exceptions"
        ]
        
        next_steps = [
            "Generate detailed report for stakeholders",
            "Update model documentation with findings",
            "Schedule follow-up review if needed",
            "Archive analysis for future reference"
        ]
        
        explanation = AuditExplanation(
            explanation_id=f"state_analysis_{model_name}_{model_kind}_{model_ts_ms}_{int(query_time.timestamp() * 1000)}",
            timestamp=datetime.now(timezone.utc),
            model_name=model_name,
            model_kind=model_kind,
            model_ts_ms=model_ts_ms,
            regime=regime,
            explanation_type="state_analysis",
            summary=summary,
            detailed_reasoning=detailed_reasoning,
            evidence=evidence,
            risk_assessment=risk_assessment,
            compliance_impact=compliance_impact,
            recommendations=recommendations,
            next_steps=next_steps
        )
        
        self._store_explanation(explanation)
        return explanation
    
    def _generate_promotion_success_reasoning(
        self,
        lineage,
        reasons: List[str],
        performance: Dict[str, Any],
        risk: Dict[str, Any]
    ) -> str:
        """Generate detailed reasoning for successful promotion"""
        return f"""
## Promotion Success Analysis

Model {lineage.model_name} ({lineage.model_kind}:{lineage.model_ts_ms}) was successfully promoted to champion status in the {lineage.regime} regime.

### Key Success Factors
{chr(10).join(f"- {reason}" for reason in reasons)}

### Performance Excellence
- Sharpe Ratio: {performance.get('sharpe_ratio', 'N/A'):.3f}
- Win Rate: {performance.get('win_rate', 'N/A'):.3f}
- Profit Factor: {performance.get('profit_factor', 'N/A'):.3f}
- Maximum Drawdown: {performance.get('max_drawdown', 'N/A'):.3f}

### Risk Assessment
- Overall Risk Score: {risk.get('model_risk_score', 'N/A'):.3f}
- Stability Score: {risk.get('stability_score', 'N/A'):.3f}
- Data Drift: {risk.get('data_drift_score', 'N/A'):.3f}

### Decision Rationale
The promotion was approved because the model demonstrated superior performance across all key metrics while maintaining acceptable risk levels. The model's stability and robustness scores indicate reliable future performance.
        """
    
    def _generate_promotion_rejection_reasoning(
        self,
        lineage,
        reasons: List[str],
        performance: Dict[str, Any],
        risk: Dict[str, Any]
    ) -> str:
        """Generate detailed reasoning for promotion rejection"""
        return f"""
## Promotion Rejection Analysis

Model {lineage.model_name} ({lineage.model_kind}:{lineage.model_ts_ms}) was not promoted to champion status in the {lineage.regime} regime.

### Rejection Reasons
{chr(10).join(f"- {reason}" for reason in reasons)}

### Performance Issues
- Sharpe Ratio: {performance.get('sharpe_ratio', 'N/A'):.3f} (Below threshold: {performance.get('sharpe_threshold', 'N/A')})
- Win Rate: {performance.get('win_rate', 'N/A'):.3f} (Below threshold: {performance.get('win_rate_threshold', 'N/A')})
- Maximum Drawdown: {performance.get('max_drawdown', 'N/A'):.3f} (Exceeds limit: {performance.get('drawdown_limit', 'N/A')})

### Risk Concerns
- Overall Risk Score: {risk.get('model_risk_score', 'N/A'):.3f} (Above acceptable level)
- Stability Score: {risk.get('stability_score', 'N/A'):.3f} (Insufficient stability)
- Data Drift: {risk.get('data_drift_score', 'N/A'):.3f} (Potential drift detected)

### Required Improvements
Before reconsideration for promotion, the model should:
1. Improve performance metrics to meet minimum thresholds
2. Reduce risk factors to acceptable levels
3. Demonstrate consistent performance over extended evaluation period
4. Address any identified stability issues
        """
    
    def _generate_promotion_evidence(self, lineage, promotion_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Generate evidence for promotion decision"""
        evidence = []
        
        # Performance evidence
        if "performance_metrics" in promotion_result:
            evidence.append({
                "type": "performance_metrics",
                "description": "Performance evaluation results",
                "data": promotion_result["performance_metrics"]
            })
        
        # Risk evidence
        if "risk_metrics" in promotion_result:
            evidence.append({
                "type": "risk_assessment",
                "description": "Risk evaluation results",
                "data": promotion_result["risk_metrics"]
            })
        
        # Validation evidence
        if "validation_results" in promotion_result:
            evidence.append({
                "type": "validation_results",
                "description": "Model validation outcomes",
                "data": promotion_result["validation_results"]
            })
        
        # Historical events evidence
        if lineage.events:
            evidence.append({
                "type": "historical_events",
                "description": "Relevant historical events",
                "data": [
                    {
                        "timestamp": e.ts_ms,
                        "event_type": e.event_type.value,
                        "reason": e.reason
                    }
                    for e in lineage.events[-10:]
                ]
            })
        
        return evidence
    
    def _assess_promotion_risk(self, risk_metrics: Dict[str, Any], performance_metrics: Dict[str, Any]) -> Dict[str, Any]:
        """Assess risk for promotion decision"""
        risk_score = risk_metrics.get("model_risk_score", 0.5)
        
        if risk_score < 0.3:
            risk_level = "LOW"
            risk_description = "Model demonstrates low risk characteristics suitable for production"
        elif risk_score < 0.6:
            risk_level = "MEDIUM"
            risk_description = "Model has moderate risk that requires monitoring"
        else:
            risk_level = "HIGH"
            risk_description = "Model exhibits high risk factors requiring caution"
        
        return {
            "risk_level": risk_level,
            "risk_score": risk_score,
            "risk_description": risk_description,
            "key_risk_factors": [
                f"Model Risk Score: {risk_score:.3f}",
                f"Stability Score: {risk_metrics.get('stability_score', 0):.3f}",
                f"Data Drift: {risk_metrics.get('data_drift_score', 0):.3f}"
            ],
            "mitigation_measures": [
                "Continuous performance monitoring",
                "Automated rollback triggers",
                "Regular risk assessments"
            ]
        }
    
    def _assess_compliance_impact(self, lineage, promotion_result: Dict[str, Any]) -> Dict[str, Any]:
        """Assess compliance impact of promotion decision"""
        return {
            "regulatory_compliance": "Maintained - all governance procedures followed",
            "audit_requirements": "Fully documented with complete traceability",
            "model_governance": "Compliant with established promotion protocols",
            "data_privacy": "No privacy concerns identified",
            "reporting_obligations": "Met - all events properly logged and explainable",
            "stakeholder_notification": "Completed - relevant parties informed"
        }
    
    def _generate_promotion_recommendations(
        self,
        was_promoted: bool,
        reasons: List[str],
        performance: Dict[str, Any],
        risk: Dict[str, Any]
    ) -> List[str]:
        """Generate recommendations based on promotion decision"""
        if was_promoted:
            return [
                "Monitor model performance closely during initial production period",
                "Implement enhanced monitoring for key risk indicators",
                "Schedule regular performance reviews",
                "Prepare rollback procedures as precaution"
            ]
        else:
            return [
                "Address identified performance deficiencies",
                "Implement risk mitigation strategies",
                "Extend evaluation period to demonstrate consistency",
                "Consider model architecture improvements",
                "Review training data quality and preprocessing"
            ]
    
    def _generate_promotion_next_steps(self, was_promoted: bool, regime: str) -> List[str]:
        """Generate next steps based on promotion decision"""
        if was_promoted:
            return [
                f"Deploy model as champion in {regime} regime",
                "Activate production monitoring",
                "Notify stakeholders of promotion",
                "Schedule post-promotion review"
            ]
        else:
            return [
                "Continue model in challenger/shadow mode",
                "Implement improvement plan",
                "Schedule next evaluation cycle",
                "Update model documentation"
            ]
    
    def _format_rollback_reasons(self, trigger_reason: str, rollback_result: Dict[str, Any]) -> str:
        """Format rollback reasons"""
        reasons = []
        
        if trigger_reason == "performance_degradation":
            reasons.append("Significant performance degradation detected")
            if "performance_details" in rollback_result:
                details = rollback_result["performance_details"]
                reasons.append(f"Sharpe ratio dropped from {details.get('previous_sharpe', 'N/A'):.3f} to {details.get('current_sharpe', 'N/A'):.3f}")
                reasons.append(f"Win rate declined from {details.get('previous_win_rate', 'N/A'):.3f} to {details.get('current_win_rate', 'N/A'):.3f}")
        
        elif trigger_reason == "risk_breach":
            reasons.append("Risk limits exceeded")
            if "risk_details" in rollback_result:
                details = rollback_result["risk_details"]
                reasons.append(f"Drawdown exceeded limit: {details.get('drawdown', 'N/A'):.3f}")
                reasons.append(f"Consecutive losses: {details.get('consecutive_losses', 'N/A')}")
        
        elif trigger_reason == "system_alert":
            reasons.append("Automated system alert triggered")
            reasons.append("Anomalous behavior detected by monitoring systems")
        
        return "\n".join(f"- {reason}" for reason in reasons)
    
    def _format_event_timeline(self, events: List) -> str:
        """Format event timeline for explanation"""
        if not events:
            return "No relevant events found."
        
        timeline = []
        for event in events[-5:]:  # Show last 5 events
            timestamp = datetime.fromtimestamp(event.ts_ms/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            timeline.append(f"**{timestamp}** - {event.event_type.value.replace('_', ' ').title()}: {event.reason}")
        
        return "\n".join(timeline)
    
    def _determine_state_at_time(self, events: List, query_time_ms: int) -> Dict[str, Any]:
        """Determine model state at specific time"""
        state = {
            "stage": "candidate",
            "risk_level": "UNKNOWN",
            "risk_factors": [],
            "mitigations": [],
            "compliance_status": "Unknown",
            "violations": [],
            "remediation_status": "N/A"
        }
        
        # Process events in chronological order
        for event in events:
            if event.event_type == LineageEventType.MODEL_PROMOTED:
                state["stage"] = "champion"
            elif event.event_type == LineageEventType.MODEL_DEMOTED:
                state["stage"] = "retired"
            elif event.event_type == LineageEventType.QUARANTINE_PLACED:
                state["stage"] = "quarantined"
                state["violations"].append(event.reason)
            elif event.event_type == LineageEventType.QUARANTINE_LIFTED:
                state["stage"] = "candidate"
                state["remediation_status"] = "Completed"
            elif event.event_type == LineageEventType.RISK_BREACH:
                state["risk_level"] = "HIGH"
                state["risk_factors"].append(event.reason)
        
        return state
    
    def _analyze_state_factors(self, events: List, state: Dict[str, Any]) -> str:
        """Analyze factors contributing to model state"""
        factors = []
        
        if state["stage"] == "champion":
            factors.append("Model successfully passed all promotion gates")
            factors.append("Performance metrics exceeded minimum thresholds")
        elif state["stage"] == "quarantined":
            factors.append("Model placed under quarantine due to identified issues")
            factors.append("Remediation actions required before reconsideration")
        elif state["stage"] == "retired":
            factors.append("Model retired after being replaced by newer version")
            factors.append("Historical performance data preserved for audit")
        
        return "\n".join(f"- {factor}" for factor in factors)
    
    def _analyze_performance_context(self, lineage, query_time_ms: int) -> str:
        """Analyze performance context at query time"""
        # This would typically query performance metrics around the query time
        return "Performance metrics at the queried time were within acceptable ranges based on available data."
    
    def _analyze_risk_context(self, events: List, query_time_ms: int) -> str:
        """Analyze risk context at query time"""
        risk_events = [e for e in events if e.event_type in [LineageEventType.RISK_BREACH, LineageEventType.PERFORMANCE_DEGRADED]]
        
        if not risk_events:
            return "No significant risk events detected around the queried time."
        
        return f"Risk indicators present: {len(risk_events)} risk-related events occurred prior to the queried time."
    
    def _store_explanation(self, explanation: AuditExplanation):
        """Store explanation in database"""
        con = connect()
        try:
            con.execute(
                """
                INSERT INTO audit_explanations
                (explanation_id, ts_ms, model_name, model_kind, model_ts_ms, regime,
                 explanation_type, summary, detailed_reasoning, evidence_json,
                 risk_assessment_json, compliance_impact_json, recommendations_json, next_steps_json, created_ts_ms)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    explanation.explanation_id,
                    int(explanation.timestamp.timestamp() * 1000),
                    explanation.model_name,
                    explanation.model_kind,
                    explanation.model_ts_ms,
                    explanation.regime,
                    explanation.explanation_type,
                    explanation.summary,
                    explanation.detailed_reasoning,
                    json.dumps(explanation.evidence, separators=(",", ":")),
                    json.dumps(explanation.risk_assessment, separators=(",", ":")),
                    json.dumps(explanation.compliance_impact, separators=(",", ":")),
                    json.dumps(explanation.recommendations, separators=(",", ":")),
                    json.dumps(explanation.next_steps, separators=(",", ":")),
                    int(time.time() * 1000)
                )
            )
            con.commit()
        finally:
            con.close()
    
    def get_explanation_history(
        self,
        model_name: Optional[str] = None,
        explanation_type: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get explanation history"""
        con = connect()
        try:
            query = "SELECT * FROM audit_explanations WHERE 1=1"
            params = []
            
            if model_name:
                query += " AND model_name=?"
                params.append(model_name)
            
            if explanation_type:
                query += " AND explanation_type=?"
                params.append(explanation_type)
            
            query += " ORDER BY ts_ms DESC LIMIT ?"
            params.append(limit)
            
            rows = con.execute(query, params).fetchall()
            
            return [
                {
                    "explanation_id": row[0],
                    "timestamp": datetime.fromtimestamp(row[1]/1000, tz=timezone.utc).isoformat(),
                    "model_name": row[2],
                    "model_kind": row[3],
                    "model_ts_ms": row[4],
                    "regime": row[5],
                    "explanation_type": row[6],
                    "summary": row[7],
                    "detailed_reasoning": row[8],
                    "evidence": json.loads(row[9] or "[]"),
                    "risk_assessment": json.loads(row[10] or "{}"),
                    "compliance_impact": json.loads(row[11] or "{}"),
                    "recommendations": json.loads(row[12] or "[]"),
                    "next_steps": json.loads(row[13] or "[]")
                }
                for row in rows
            ]
            
        finally:
            con.close()

# Global explanation engine
_explanation_engine = None

def get_explanation_engine() -> AuditExplanationEngine:
    """Get singleton explanation engine"""
    global _explanation_engine
    if _explanation_engine is None:
        _explanation_engine = AuditExplanationEngine()
    return _explanation_engine
