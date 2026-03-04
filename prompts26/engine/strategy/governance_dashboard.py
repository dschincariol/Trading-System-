"""
Model Governance Dashboard and Reporting System
Provides web-based dashboard and exportable audit reports.
"""

import json
import time
import os
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict
from enum import Enum

from engine.storage import connect, init_db
from engine.strategy.model_lineage import get_lineage_tracker
from engine.strategy.audit_explanations import get_explanation_engine
from engine.strategy.model_registry import list_recent, get_stage_latest

class ReportFormat(Enum):
    JSON = "json"
    CSV = "csv"
    PDF = "pdf"
    HTML = "html"

class TimeRange(Enum):
    LAST_24H = "24h"
    LAST_7D = "7d"
    LAST_30D = "30d"
    LAST_90D = "90d"
    CUSTOM = "custom"

@dataclass
class DashboardMetrics:
    """Dashboard metrics summary"""
    total_models: int
    active_champions: int
    pending_promotions: int
    recent_rollbacks: int
    compliance_issues: int
    high_risk_models: int
    avg_performance_score: float
    avg_risk_score: float

@dataclass
class ModelSummary:
    """Summary information for a model"""
    model_name: str
    current_stage: str
    model_kind: str
    model_ts_ms: int
    regime: str
    performance_score: float
    risk_score: float
    last_updated: datetime
    days_in_stage: int
    promotion_count: int
    rollback_count: int

class ModelGovernanceDashboard:
    """Comprehensive dashboard for model governance"""
    
    def __init__(self):
        self.lineage_tracker = get_lineage_tracker()
        self.explanation_engine = get_explanation_engine()
        self._init_dashboard_tables()
    
    def _init_dashboard_tables(self):
        """Initialize dashboard-specific tables"""
        init_db()
        con = connect()
        try:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS dashboard_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    snapshot_ts_ms INTEGER NOT NULL,
                    metrics_json TEXT NOT NULL,
                    created_ts_ms INTEGER NOT NULL
                );
                
                CREATE TABLE IF NOT EXISTS exportable_reports (
                    report_id TEXT PRIMARY KEY,
                    report_type TEXT NOT NULL,
                    model_name TEXT,
                    regime TEXT,
                    time_range TEXT,
                    format TEXT NOT NULL,
                    file_path TEXT,
                    generated_ts_ms INTEGER NOT NULL,
                    expires_ts_ms INTEGER,
                    download_count INTEGER DEFAULT 0
                );
                
                CREATE INDEX IF NOT EXISTS idx_dashboard_snapshots_time 
                    ON dashboard_snapshots(snapshot_ts_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_exportable_reports_model 
                    ON exportable_reports(model_name, generated_ts_ms DESC);
            """)
            con.commit()
        finally:
            con.close()
    
    def get_dashboard_metrics(
        self,
        regime: str = "global",
        time_range: TimeRange = TimeRange.LAST_30D
    ) -> DashboardMetrics:
        """Get comprehensive dashboard metrics"""
        con = connect()
        try:
            # Calculate time filter
            time_filter_ms = self._get_time_filter_ms(time_range)
            
            # Get total models
            total_models = con.execute(
                """
                SELECT COUNT(DISTINCT model_name) 
                FROM model_registry 
                WHERE regime=? AND created_ts_ms >= ?
                """,
                (regime, time_filter_ms)
            ).fetchone()[0]
            
            # Get active champions
            active_champions = con.execute(
                """
                SELECT COUNT(*) 
                FROM model_registry 
                WHERE regime=? AND stage='champion'
                """,
                (regime,)
            ).fetchone()[0]
            
            # Get pending promotions (challengers)
            pending_promotions = con.execute(
                """
                SELECT COUNT(*) 
                FROM model_registry 
                WHERE regime=? AND stage='challenger'
                """,
                (regime,)
            ).fetchone()[0]
            
            # Get recent rollbacks
            recent_rollbacks = con.execute(
                """
                SELECT COUNT(*) 
                FROM rollback_history 
                WHERE regime=? AND ts_ms >= ?
                """,
                (regime, time_filter_ms)
            ).fetchone()[0]
            
            # Get compliance issues (quarantined models)
            compliance_issues = con.execute(
                """
                SELECT COUNT(*) 
                FROM model_registry 
                WHERE regime=? AND stage='quarantined'
                """,
                (regime,)
            ).fetchone()[0]
            
            # Get high risk models (based on metrics)
            high_risk_models = con.execute(
                """
                SELECT COUNT(*) 
                FROM model_registry 
                WHERE regime=? AND 
                      json_extract(metrics_json, '$.risk_score') > 0.7
                """,
                (regime,)
            ).fetchone()[0] or 0
            
            # Calculate average performance and risk scores
            avg_performance = con.execute(
                """
                SELECT AVG(CAST(json_extract(metrics_json, '$.sharpe') AS REAL))
                FROM model_registry 
                WHERE regime=? AND stage='champion' AND 
                      json_extract(metrics_json, '$.sharpe') IS NOT NULL
                """,
                (regime,)
            ).fetchone()[0] or 0.0
            
            avg_risk = con.execute(
                """
                SELECT AVG(CAST(json_extract(metrics_json, '$.risk_score') AS REAL))
                FROM model_registry 
                WHERE regime=? AND 
                      json_extract(metrics_json, '$.risk_score') IS NOT NULL
                """,
                (regime,)
            ).fetchone()[0] or 0.0
            
            return DashboardMetrics(
                total_models=total_models,
                active_champions=active_champions,
                pending_promotions=pending_promotions,
                recent_rollbacks=recent_rollbacks,
                compliance_issues=compliance_issues,
                high_risk_models=high_risk_models,
                avg_performance_score=float(avg_performance),
                avg_risk_score=float(avg_risk)
            )
            
        finally:
            con.close()
    
    def get_model_summaries(
        self,
        regime: str = "global",
        stage_filter: Optional[str] = None,
        limit: int = 50
    ) -> List[ModelSummary]:
        """Get summary information for models"""
        con = connect()
        try:
            query = """
                SELECT DISTINCT model_name, stage, model_kind, model_ts_ms, 
                       metrics_json, created_ts_ms
                FROM model_registry 
                WHERE regime=?
            """
            params = [regime]
            
            if stage_filter:
                query += " AND stage=?"
                params.append(stage_filter)
            
            query += " ORDER BY created_ts_ms DESC LIMIT ?"
            params.append(limit)
            
            rows = con.execute(query, params).fetchall()
            
            summaries = []
            for model_name, stage, model_kind, model_ts_ms, metrics_json, created_ts_ms in rows:
                metrics = json.loads(metrics_json or "{}")
                
                # Calculate days in current stage
                days_in_stage = (int(time.time() * 1000) - created_ts_ms) // (24 * 60 * 60 * 1000)
                
                # Get promotion and rollback counts
                promotion_count = con.execute(
                    """
                    SELECT COUNT(*) FROM model_lineage_events 
                    WHERE model_name=? AND regime=? AND event_type='model_promoted'
                    """,
                    (model_name, regime)
                ).fetchone()[0] or 0
                
                rollback_count = con.execute(
                    """
                    SELECT COUNT(*) FROM model_lineage_events 
                    WHERE model_name=? AND regime=? AND event_type='rollback_executed'
                    """,
                    (model_name, regime)
                ).fetchone()[0] or 0
                
                summaries.append(ModelSummary(
                    model_name=model_name,
                    current_stage=stage,
                    model_kind=model_kind,
                    model_ts_ms=model_ts_ms,
                    regime=regime,
                    performance_score=float(metrics.get('sharpe', 0)),
                    risk_score=float(metrics.get('risk_score', 0)),
                    last_updated=datetime.fromtimestamp(created_ts_ms/1000, tz=timezone.utc),
                    days_in_stage=days_in_stage,
                    promotion_count=promotion_count,
                    rollback_count=rollback_count
                ))
            
            return summaries
            
        finally:
            con.close()
    
    def get_promotion_pipeline(
        self,
        regime: str = "global"
    ) -> Dict[str, List[ModelSummary]]:
        """Get models in promotion pipeline by stage"""
        pipeline = {
            "candidates": [],
            "challengers": [],
            "champions": [],
            "retired": [],
            "quarantined": []
        }
        
        for stage in pipeline.keys():
            pipeline[stage] = self.get_model_summaries(regime, stage_filter=stage, limit=20)
        
        return pipeline
    
    def get_recent_activity(
        self,
        regime: str = "global",
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get recent governance activity"""
        con = connect()
        try:
            # Get recent lineage events
            events = con.execute(
                """
                SELECT event_type, model_name, model_kind, model_ts_ms, 
                       ts_ms, actor, reason
                FROM model_lineage_events 
                WHERE regime=?
                ORDER BY ts_ms DESC 
                LIMIT ?
                """,
                (regime, limit)
            ).fetchall()
            
            activity = []
            for event_type, model_name, model_kind, model_ts_ms, ts_ms, actor, reason in events:
                activity.append({
                    "timestamp": datetime.fromtimestamp(ts_ms/1000, tz=timezone.utc).isoformat(),
                    "event_type": event_type,
                    "model_name": model_name,
                    "model_kind": model_kind,
                    "model_ts_ms": model_ts_ms,
                    "actor": actor,
                    "reason": reason,
                    "event_description": event_type.replace('_', ' ').title()
                })
            
            return activity
            
        finally:
            con.close()
    
    def generate_audit_report(
        self,
        model_name: Optional[str] = None,
        regime: str = "global",
        time_range: TimeRange = TimeRange.LAST_30D,
        format: ReportFormat = ReportFormat.JSON
    ) -> str:
        """Generate comprehensive audit report"""
        time_filter_ms = self._get_time_filter_ms(time_range)
        
        # Collect report data
        report_data = {
            "report_metadata": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "regime": regime,
                "time_range": time_range.value,
                "model_filter": model_name
            },
            "dashboard_metrics": asdict(self.get_dashboard_metrics(regime, time_range)),
            "promotion_pipeline": {
                stage: [asdict(model) for model in models]
                for stage, models in self.get_promotion_pipeline(regime).items()
            },
            "recent_activity": self.get_recent_activity(regime, limit=100),
            "explanations": self.explanation_engine.get_explanation_history(
                model_name, limit=50
            )
        }
        
        if model_name:
            # Add model-specific details
            model_history = self.lineage_tracker.get_model_history(model_name, regime)
            report_data["model_history"] = model_history
            
            # Get lineage for latest model version
            if model_history:
                latest = model_history[0]
                lineage = self.lineage_tracker.get_model_lineage(
                    model_name, latest["model_kind"], latest["model_ts_ms"], regime
                )
                report_data["model_lineage"] = {
                    "events": [
                        {
                            "timestamp": datetime.fromtimestamp(e.ts_ms/1000, tz=timezone.utc).isoformat(),
                            "event_type": e.event_type.value,
                            "actor": e.actor,
                            "reason": e.reason
                        }
                        for e in lineage.events
                    ],
                    "current_stage": lineage.current_stage.value,
                    "parent_models": lineage.parent_models,
                    "child_models": lineage.child_models
                }
        
        # Generate report based on format
        if format == ReportFormat.JSON:
            return self._generate_json_report(report_data)
        elif format == ReportFormat.CSV:
            return self._generate_csv_report(report_data)
        elif format == ReportFormat.HTML:
            return self._generate_html_report(report_data)
        elif format == ReportFormat.PDF:
            return self._generate_pdf_report(report_data)
        else:
            raise ValueError(f"Unsupported report format: {format}")
    
    def export_model_lineage(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        regime: str = "global",
        format: ReportFormat = ReportFormat.JSON
    ) -> str:
        """Export complete model lineage"""
        lineage = self.lineage_tracker.get_model_lineage(
            model_name, model_kind, model_ts_ms, regime
        )
        
        explanation = self.lineage_tracker.explain_model_state(
            model_name, model_kind, model_ts_ms, regime
        )
        
        lineage_data = {
            "model_info": {
                "model_name": lineage.model_name,
                "model_kind": lineage.model_kind,
                "model_ts_ms": lineage.model_ts_ms,
                "regime": lineage.regime,
                "current_stage": lineage.current_stage.value,
                "created_ts_ms": lineage.created_ts_ms
            },
            "training_data": asdict(lineage.training_data) if lineage.training_data else None,
            "events": [
                {
                    "event_id": e.event_id,
                    "timestamp": datetime.fromtimestamp(e.ts_ms/1000, tz=timezone.utc).isoformat(),
                    "event_type": e.event_type.value,
                    "actor": e.actor,
                    "reason": e.reason,
                    "metadata": e.metadata
                }
                for e in lineage.events
            ],
            "parent_models": lineage.parent_models,
            "child_models": lineage.child_models,
            "human_readable_explanation": explanation
        }
        
        if format == ReportFormat.JSON:
            return json.dumps(lineage_data, indent=2, default=str)
        elif format == ReportFormat.HTML:
            return self._generate_lineage_html_report(lineage_data)
        else:
            return json.dumps(lineage_data, indent=2, default=str)
    
    def save_dashboard_snapshot(self) -> str:
        """Save current dashboard state as snapshot"""
        metrics = self.get_dashboard_metrics()
        snapshot_id = f"dashboard_snapshot_{int(time.time() * 1000)}"
        
        con = connect()
        try:
            con.execute(
                """
                INSERT INTO dashboard_snapshots
                (snapshot_id, snapshot_ts_ms, metrics_json, created_ts_ms)
                VALUES (?,?,?,?)
                """,
                (
                    snapshot_id,
                    int(time.time() * 1000),
                    json.dumps(asdict(metrics), separators=(",", ":")),
                    int(time.time() * 1000)
                )
            )
            con.commit()
            return snapshot_id
        finally:
            con.close()
    
    def get_dashboard_history(
        self,
        days: int = 30
    ) -> List[Dict[str, Any]]:
        """Get dashboard metrics history"""
        con = connect()
        try:
            time_filter_ms = int((time.time() - days * 24 * 60 * 60) * 1000)
            
            rows = con.execute(
                """
                SELECT snapshot_id, snapshot_ts_ms, metrics_json
                FROM dashboard_snapshots
                WHERE snapshot_ts_ms >= ?
                ORDER BY snapshot_ts_ms ASC
                """,
                (time_filter_ms,)
            ).fetchall()
            
            history = []
            for snapshot_id, snapshot_ts_ms, metrics_json in rows:
                history.append({
                    "snapshot_id": snapshot_id,
                    "timestamp": datetime.fromtimestamp(snapshot_ts_ms/1000, tz=timezone.utc).isoformat(),
                    "metrics": json.loads(metrics_json)
                })
            
            return history
            
        finally:
            con.close()
    
    def _get_time_filter_ms(self, time_range: TimeRange) -> int:
        """Get timestamp filter for time range"""
        now_ms = int(time.time() * 1000)
        
        if time_range == TimeRange.LAST_24H:
            return now_ms - (24 * 60 * 60 * 1000)
        elif time_range == TimeRange.LAST_7D:
            return now_ms - (7 * 24 * 60 * 60 * 1000)
        elif time_range == TimeRange.LAST_30D:
            return now_ms - (30 * 24 * 60 * 60 * 1000)
        elif time_range == TimeRange.LAST_90D:
            return now_ms - (90 * 24 * 60 * 60 * 1000)
        else:
            return 0  # All time
    
    def _generate_json_report(self, data: Dict[str, Any]) -> str:
        """Generate JSON format report"""
        return json.dumps(data, indent=2, default=str)
    
    def _generate_csv_report(self, data: Dict[str, Any]) -> str:
        """Generate CSV format report"""
        import csv
        import io
        
        output = io.StringIO()
        
        # Dashboard metrics
        if "dashboard_metrics" in data:
            output.write("# Dashboard Metrics\n")
            metrics = data["dashboard_metrics"]
            writer = csv.writer(output)
            writer.writerow(["Metric", "Value"])
            for key, value in metrics.items():
                writer.writerow([key.replace("_", " ").title(), value])
            output.write("\n")
        
        # Recent activity
        if "recent_activity" in data:
            output.write("# Recent Activity\n")
            writer = csv.writer(output)
            writer.writerow(["Timestamp", "Event Type", "Model Name", "Actor", "Reason"])
            for activity in data["recent_activity"]:
                writer.writerow([
                    activity["timestamp"],
                    activity["event_type"],
                    activity["model_name"],
                    activity["actor"],
                    activity["reason"]
                ])
        
        return output.getvalue()
    
    def _generate_html_report(self, data: Dict[str, Any]) -> str:
        """Generate HTML format report"""
        html = """
<!DOCTYPE html>
<html>
<head>
    <title>Model Governance Audit Report</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        .header { background-color: #f0f0f0; padding: 20px; border-radius: 5px; }
        .metrics { display: flex; flex-wrap: wrap; gap: 20px; margin: 20px 0; }
        .metric-card { background-color: #f9f9f9; padding: 15px; border-radius: 5px; min-width: 200px; }
        .metric-value { font-size: 24px; font-weight: bold; color: #2c3e50; }
        .metric-label { font-size: 14px; color: #7f8c8d; }
        table { width: 100%; border-collapse: collapse; margin: 20px 0; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
        th { background-color: #f2f2f2; }
        .event-promotion { background-color: #d4edda; }
        .event-rollback { background-color: #f8d7da; }
        .event-compliance { background-color: #fff3cd; }
    </style>
</head>
<body>
    <div class="header">
        <h1>Model Governance Audit Report</h1>
        <p><strong>Generated:</strong> {generated_at}</p>
        <p><strong>Regime:</strong> {regime}</p>
        <p><strong>Time Range:</strong> {time_range}</p>
    </div>
        
    <h2>Dashboard Metrics</h2>
    <div class="metrics">
        {metrics_cards}
    </div>
        
    <h2>Recent Activity</h2>
    <table>
        <tr>
            <th>Timestamp</th>
            <th>Event Type</th>
            <th>Model Name</th>
            <th>Actor</th>
            <th>Reason</th>
        </tr>
        {activity_rows}
    </table>
</body>
</html>
        """
        
        # Generate metrics cards
        metrics = data.get("dashboard_metrics", {})
        metrics_cards = ""
        for key, value in metrics.items():
            label = key.replace("_", " ").title()
            metrics_cards += f"""
            <div class="metric-card">
                <div class="metric-value">{value}</div>
                <div class="metric-label">{label}</div>
            </div>
            """
        
        # Generate activity rows
        activity_rows = ""
        for activity in data.get("recent_activity", []):
            event_type = activity["event_type"]
            row_class = ""
            if "promotion" in event_type:
                row_class = "event-promotion"
            elif "rollback" in event_type:
                row_class = "event-rollback"
            elif "compliance" in event_type:
                row_class = "event-compliance"
            
            activity_rows += f"""
            <tr class="{row_class}">
                <td>{activity["timestamp"]}</td>
                <td>{activity["event_description"]}</td>
                <td>{activity["model_name"]}</td>
                <td>{activity["actor"]}</td>
                <td>{activity["reason"]}</td>
            </tr>
            """
        
        return html.format(
            generated_at=data["report_metadata"]["generated_at"],
            regime=data["report_metadata"]["regime"],
            time_range=data["report_metadata"]["time_range"],
            metrics_cards=metrics_cards,
            activity_rows=activity_rows
        )
    
    def _generate_pdf_report(self, data: Dict[str, Any]) -> str:
        """Generate PDF format report (placeholder)"""
        # In a real implementation, this would use a PDF library like ReportLab
        return "PDF generation not implemented in this demo"
    
    def _generate_lineage_html_report(self, lineage_data: Dict[str, Any]) -> str:
        """Generate HTML format lineage report"""
        model_info = lineage_data["model_info"]
        
        html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Model Lineage Report - {model_info['model_name']}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        .header {{ background-color: #f0f0f0; padding: 20px; border-radius: 5px; }}
        .section {{ margin: 20px 0; }}
        .event {{ background-color: #f9f9f9; padding: 10px; margin: 5px 0; border-left: 4px solid #007bff; }}
        .explanation {{ background-color: #e7f3ff; padding: 15px; border-radius: 5px; white-space: pre-wrap; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Model Lineage Report</h1>
        <p><strong>Model:</strong> {model_info['model_name']}</p>
        <p><strong>Version:</strong> {model_info['model_kind']}:{model_info['model_ts_ms']}</p>
        <p><strong>Regime:</strong> {model_info['regime']}</p>
        <p><strong>Current Stage:</strong> {model_info['current_stage']}</p>
    </div>
    
    <div class="section">
        <h2>Event Timeline</h2>
        {event_timeline}
    </div>
    
    <div class="section">
        <h2>Human-Readable Explanation</h2>
        <div class="explanation">{explanation}</div>
    </div>
</body>
</html>
        """
        
        # Generate event timeline
        event_timeline = ""
        for event in lineage_data["events"]:
            event_timeline += f"""
            <div class="event">
                <strong>{event['timestamp']}</strong> - {event['event_type'].replace('_', ' ').title()}<br>
                <strong>Actor:</strong> {event['actor']}<br>
                <strong>Reason:</strong> {event['reason']}
            </div>
            """
        
        explanation = lineage_data.get("human_readable_explanation", "No explanation available")
        
        return html.format(event_timeline=event_timeline, explanation=explanation)

# Global dashboard instance
_dashboard_instance = None

def get_dashboard() -> ModelGovernanceDashboard:
    """Get singleton dashboard instance"""
    global _dashboard_instance
    if _dashboard_instance is None:
        _dashboard_instance = ModelGovernanceDashboard()
    return _dashboard_instance
