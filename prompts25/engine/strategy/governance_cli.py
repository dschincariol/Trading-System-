#!/usr/bin/env python3
"""
Model Governance CLI
Command-line interface for model governance and audit intelligence.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Dict, Any

from engine.strategy.governance_dashboard import get_dashboard, ReportFormat, TimeRange
from engine.strategy.model_lineage import get_lineage_tracker, TrainingDataInfo
from engine.strategy.audit_explanations import get_explanation_engine
from engine.strategy.model_registry import (
    register_model, promote_to_champion, rollback_champion,
    get_stage_latest, list_recent
)

def cmd_dashboard(args):
    """Show dashboard metrics"""
    dashboard = get_dashboard()
    metrics = dashboard.get_dashboard_metrics(args.regime, TimeRange(args.time_range))
    
    print(f"\n📊 Model Governance Dashboard - {args.regime} regime")
    print(f"📅 Time Range: {args.time_range}")
    print("=" * 60)
    print(f"Total Models:           {metrics.total_models}")
    print(f"Active Champions:       {metrics.active_champions}")
    print(f"Pending Promotions:     {metrics.pending_promotions}")
    print(f"Recent Rollbacks:       {metrics.recent_rollbacks}")
    print(f"Compliance Issues:      {metrics.compliance_issues}")
    print(f"High Risk Models:       {metrics.high_risk_models}")
    print(f"Avg Performance Score:  {metrics.avg_performance_score:.3f}")
    print(f"Avg Risk Score:         {metrics.avg_risk_score:.3f}")
    print("=" * 60)

def cmd_pipeline(args):
    """Show promotion pipeline"""
    dashboard = get_dashboard()
    pipeline = dashboard.get_promotion_pipeline(args.regime)
    
    print(f"\n🚀 Promotion Pipeline - {args.regime} regime")
    print("=" * 60)
    
    for stage, models in pipeline.items():
        if models:
            print(f"\n{stage.upper()} ({len(models)} models):")
            for model in models[:5]:  # Show top 5
                print(f"  • {model.model_name} - Performance: {model.performance_score:.3f}, Risk: {model.risk_score:.3f}")
            if len(models) > 5:
                print(f"  ... and {len(models) - 5} more")

def cmd_activity(args):
    """Show recent activity"""
    dashboard = get_dashboard()
    activity = dashboard.get_recent_activity(args.regime, args.limit)
    
    print(f"\n📋 Recent Activity - {args.regime} regime")
    print("=" * 80)
    
    for event in activity:
        timestamp = datetime.fromisoformat(event["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{timestamp} | {event['event_description']:20} | {event['model_name']:15} | {event['actor']}")
        if event['reason'] and len(event['reason']) < 50:
            print(f"          Reason: {event['reason']}")

def cmd_explain(args):
    """Explain model state"""
    lineage_tracker = get_lineage_tracker()
    
    try:
        explanation = lineage_tracker.explain_model_state(
            args.model_name, args.model_kind, args.model_ts_ms, args.regime
        )
        print(f"\n🔍 Model State Explanation")
        print("=" * 60)
        print(explanation)
    except Exception as e:
        print(f"❌ Error generating explanation: {e}")

def cmd_lineage(args):
    """Show model lineage"""
    lineage_tracker = get_lineage_tracker()
    
    try:
        lineage = lineage_tracker.get_model_lineage(
            args.model_name, args.model_kind, args.model_ts_ms, args.regime
        )
        
        print(f"\n🧬 Model Lineage")
        print("=" * 60)
        print(f"Model: {lineage.model_name} ({lineage.model_kind}:{lineage.model_ts_ms})")
        print(f"Regime: {lineage.regime}")
        print(f"Current Stage: {lineage.current_stage.value}")
        print(f"Total Events: {len(lineage.events)}")
        
        if lineage.training_data:
            print(f"\n📚 Training Data:")
            print(f"  Version: {lineage.training_data.data_version}")
            print(f"  Source: {lineage.training_data.data_source}")
            print(f"  Samples: {lineage.training_data.sample_count:,}")
            print(f"  Features: {lineage.training_data.feature_count}")
        
        print(f"\n📜 Recent Events:")
        for event in lineage.events[-10:]:
            timestamp = datetime.fromtimestamp(event.ts_ms/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            print(f"  {timestamp} | {event.event_type.value.replace('_', ' ').title()} | {event.actor}")
            print(f"               {event.reason}")
        
    except Exception as e:
        print(f"❌ Error retrieving lineage: {e}")

def cmd_register(args):
    """Register a new model"""
    try:
        # Create training data info if provided
        training_data = None
        if args.data_version:
            training_data = TrainingDataInfo(
                data_version=args.data_version,
                data_source=args.data_source or "unknown",
                data_hash=args.data_hash or "",
                sample_count=args.sample_count or 0,
                feature_count=args.feature_count or 0,
                training_start_ts_ms=args.training_start_ms or 0,
                training_end_ts_ms=args.training_end_ms or 0,
                preprocessing_config={},
                data_quality_metrics={}
            )
        
        # Load metrics from file if provided
        metrics = {}
        if args.metrics_file:
            with open(args.metrics_file, 'r') as f:
                metrics = json.load(f)
        
        register_model(
            model_name=args.model_name,
            model_kind=args.model_kind,
            model_ts_ms=args.model_ts_ms,
            stage=args.stage,
            metrics=metrics,
            note=args.note,
            regime=args.regime,
            actor=args.actor,
            training_data=training_data
        )
        
        print(f"✅ Successfully registered model {args.model_name}")
        
    except Exception as e:
        print(f"❌ Error registering model: {e}")

def cmd_promote(args):
    """Promote a model to champion"""
    try:
        result = promote_to_champion(
            args.model_name,
            args.model_kind,
            args.model_ts_ms,
            regime=args.regime,
            actor=args.actor,
            generate_explanation=True
        )
        
        if result is None:
            print(f"✅ Successfully promoted {args.model_name} to champion")
        else:
            from_kind, from_ts = result
            print(f"✅ Successfully promoted {args.model_name} to champion")
            print(f"   Previous champion: {from_kind}:{from_ts}")
            
    except Exception as e:
        print(f"❌ Error promoting model: {e}")

def cmd_rollback(args):
    """Rollback champion model"""
    try:
        new_champion = rollback_champion(
            args.model_name,
            regime=args.regime,
            actor=args.actor,
            reason=args.reason,
            generate_explanation=True
        )
        
        if new_champion:
            print(f"✅ Successfully rolled back {args.model_name}")
            print(f"   New champion: {new_champion['model_kind']}:{new_champion['model_ts_ms']}")
        else:
            print(f"❌ No rollback candidate found for {args.model_name}")
            
    except Exception as e:
        print(f"❌ Error rolling back model: {e}")

def cmd_report(args):
    """Generate audit report"""
    dashboard = get_dashboard()
    
    try:
        report = dashboard.generate_audit_report(
            model_name=args.model_name,
            regime=args.regime,
            time_range=TimeRange(args.time_range),
            format=ReportFormat(args.format)
        )
        
        if args.output:
            with open(args.output, 'w') as f:
                f.write(report)
            print(f"✅ Report saved to {args.output}")
        else:
            print(report)
            
    except Exception as e:
        print(f"❌ Error generating report: {e}")

def cmd_export(args):
    """Export model lineage"""
    dashboard = get_dashboard()
    
    try:
        export_data = dashboard.export_model_lineage(
            args.model_name,
            args.model_kind,
            args.model_ts_ms,
            args.regime,
            format=ReportFormat(args.format)
        )
        
        if args.output:
            with open(args.output, 'w') as f:
                f.write(export_data)
            print(f"✅ Lineage export saved to {args.output}")
        else:
            print(export_data)
            
    except Exception as e:
        print(f"❌ Error exporting lineage: {e}")

def main():
    parser = argparse.ArgumentParser(description="Model Governance CLI")
    parser.add_argument("--regime", default="global", help="Model regime (default: global)")
    parser.add_argument("--actor", default="cli_user", help="Actor name for audit trail")
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Dashboard command
    dashboard_parser = subparsers.add_parser("dashboard", help="Show dashboard metrics")
    dashboard_parser.add_argument("--time-range", default="30d", choices=["24h", "7d", "30d", "90d"])
    dashboard_parser.set_defaults(func=cmd_dashboard)
    
    # Pipeline command
    pipeline_parser = subparsers.add_parser("pipeline", help="Show promotion pipeline")
    pipeline_parser.set_defaults(func=cmd_pipeline)
    
    # Activity command
    activity_parser = subparsers.add_parser("activity", help="Show recent activity")
    activity_parser.add_argument("--limit", type=int, default=20)
    activity_parser.set_defaults(func=cmd_activity)
    
    # Explain command
    explain_parser = subparsers.add_parser("explain", help="Explain model state")
    explain_parser.add_argument("model_name", help="Model name")
    explain_parser.add_argument("model_kind", help="Model kind")
    explain_parser.add_argument("model_ts_ms", type=int, help="Model timestamp")
    explain_parser.set_defaults(func=cmd_explain)
    
    # Lineage command
    lineage_parser = subparsers.add_parser("lineage", help="Show model lineage")
    lineage_parser.add_argument("model_name", help="Model name")
    lineage_parser.add_argument("model_kind", help="Model kind")
    lineage_parser.add_argument("model_ts_ms", type=int, help="Model timestamp")
    lineage_parser.set_defaults(func=cmd_lineage)
    
    # Register command
    register_parser = subparsers.add_parser("register", help="Register a model")
    register_parser.add_argument("model_name", help="Model name")
    register_parser.add_argument("model_kind", help="Model kind")
    register_parser.add_argument("model_ts_ms", type=int, help="Model timestamp")
    register_parser.add_argument("--stage", default="candidate", help="Model stage")
    register_parser.add_argument("--note", help="Registration note")
    register_parser.add_argument("--metrics-file", help="JSON file with metrics")
    register_parser.add_argument("--data-version", help="Training data version")
    register_parser.add_argument("--data-source", help="Training data source")
    register_parser.add_argument("--data-hash", help="Training data hash")
    register_parser.add_argument("--sample-count", type=int, help="Training sample count")
    register_parser.add_argument("--feature-count", type=int, help="Feature count")
    register_parser.add_argument("--training-start-ms", type=int, help="Training start timestamp")
    register_parser.add_argument("--training-end-ms", type=int, help="Training end timestamp")
    register_parser.set_defaults(func=cmd_register)
    
    # Promote command
    promote_parser = subparsers.add_parser("promote", help="Promote model to champion")
    promote_parser.add_argument("model_name", help="Model name")
    promote_parser.add_argument("model_kind", help="Model kind")
    promote_parser.add_argument("model_ts_ms", type=int, help="Model timestamp")
    promote_parser.set_defaults(func=cmd_promote)
    
    # Rollback command
    rollback_parser = subparsers.add_parser("rollback", help="Rollback champion model")
    rollback_parser.add_argument("model_name", help="Model name")
    rollback_parser.add_argument("--reason", default="manual_rollback", help="Rollback reason")
    rollback_parser.set_defaults(func=cmd_rollback)
    
    # Report command
    report_parser = subparsers.add_parser("report", help="Generate audit report")
    report_parser.add_argument("--model-name", help="Specific model name (optional)")
    report_parser.add_argument("--time-range", default="30d", choices=["24h", "7d", "30d", "90d"])
    report_parser.add_argument("--format", default="json", choices=["json", "csv", "html"], help="Report format")
    report_parser.add_argument("--output", help="Output file (optional)")
    report_parser.set_defaults(func=cmd_report)
    
    # Export command
    export_parser = subparsers.add_parser("export", help="Export model lineage")
    export_parser.add_argument("model_name", help="Model name")
    export_parser.add_argument("model_kind", help="Model kind")
    export_parser.add_argument("model_ts_ms", type=int, help="Model timestamp")
    export_parser.add_argument("--format", default="json", choices=["json", "html"], help="Export format")
    export_parser.add_argument("--output", help="Output file (optional)")
    export_parser.set_defaults(func=cmd_export)
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    args.func(args)

if __name__ == "__main__":
    main()
