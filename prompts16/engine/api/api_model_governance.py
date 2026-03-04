"""
Model Governance API Endpoints
Operator visibility and control interfaces for the production model governance system.
"""

import json
import time
from typing import Dict, Any, List, Optional

from engine.api.internal_access import db_connect
from engine.strategy.model_governance import get_governance, PromotionDecision
from engine.strategy.champion_challenger import get_champion_challenger_manager
from engine.strategy.automatic_demotion import get_demotion_manager
from engine.strategy.rollback_manager import get_rollback_manager, RollbackStrategy, RollbackReason

def api_get_model_governance_status(parsed=None, ctx=None):
    """Get comprehensive model governance status"""
    try:
        governance = get_governance()
        cc_manager = get_champion_challenger_manager()
        demotion_manager = get_demotion_manager()
        rollback_manager = get_rollback_manager()
        
        # Get system overview
        con = db_connect()
        
        # Model counts by stage
        model_counts = {}
        stages = ["champion", "challenger", "shadow", "retired", "quarantined"]
        for stage in stages:
            count = con.execute(
                "SELECT COUNT(DISTINCT model_name) FROM model_registry WHERE stage=?",
                (stage,)
            ).fetchone()[0]
            model_counts[stage] = count
        
        # Recent governance actions
        recent_actions = con.execute(
            """
            SELECT ts_ms, model_name, action, decision, reason
            FROM model_governance_log
            ORDER BY ts_ms DESC
            LIMIT 10
            """
        ).fetchall()
        
        # Active alerts
        active_alerts = con.execute(
            """
            SELECT COUNT(*) FROM model_health_monitor
            WHERE ts_ms > ? AND severity IN ('HIGH', 'CRITICAL')
            """,
            (int(time.time() * 1000) - 3600000,)  # Last hour
        ).fetchone()[0]
        
        con.close()
        
        return {
            "ok": True,
            "timestamp": int(time.time() * 1000),
            "model_counts": model_counts,
            "active_alerts": active_alerts,
            "recent_actions": [
                {
                    "ts_ms": row[0],
                    "model_name": row[1],
                    "action": row[2],
                    "decision": row[3],
                    "reason": row[4]
                }
                for row in recent_actions
            ],
            "system_health": {
                "governance_active": True,
                "auto_promotion_enabled": cc_manager.config.auto_promote_enabled,
                "auto_demotion_enabled": demotion_manager.config.auto_rollback_enabled,
                "auto_rollback_enabled": rollback_manager.config.auto_rollback_enabled
            }
        }
        
    except Exception as e:
        return {"ok": False, "error": str(e)}

def api_get_model_details(parsed=None, ctx=None):
    """Get detailed information about a specific model"""
    try:
        model_name = parsed.get("model_name") if parsed else None
        regime = parsed.get("regime", "global") if parsed else "global"
        
        if not model_name:
            return {"ok": False, "error": "model_name required"}
        
        con = db_connect()
        
        # Get model records across all stages
        model_records = con.execute(
            """
            SELECT model_kind, model_ts_ms, stage, metrics_json, created_ts_ms, note
            FROM model_registry
            WHERE model_name=? AND regime=?
            ORDER BY created_ts_ms DESC
            """,
            (model_name, regime)
        ).fetchall()
        
        # Get governance history
        governance_history = con.execute(
            """
            SELECT ts_ms, action, decision, reason, metrics_json
            FROM model_governance_log
            WHERE model_name=? AND regime=?
            ORDER BY ts_ms DESC
            LIMIT 20
            """,
            (model_name, regime)
        ).fetchall()
        
        # Get health monitoring data
        health_data = con.execute(
            """
            SELECT ts_ms, health_score, violation_type, violation_value, severity
            FROM model_health_monitor
            WHERE model_name=?
            ORDER BY ts_ms DESC
            LIMIT 50
            """,
            (model_name,)
        ).fetchall()
        
        # Get rollback history
        rollback_history = con.execute(
            """
            SELECT ts_ms, strategy, reason, rollback_duration_ms, success, error_message
            FROM rollback_history
            WHERE model_name=? AND regime=?
            ORDER BY ts_ms DESC
            LIMIT 10
            """,
            (model_name, regime)
        ).fetchall()
        
        con.close()
        
        return {
            "ok": True,
            "model_name": model_name,
            "regime": regime,
            "records": [
                {
                    "model_kind": row[0],
                    "model_ts_ms": row[1],
                    "stage": row[2],
                    "metrics": json.loads(row[3] or "{}"),
                    "created_ts_ms": row[4],
                    "note": row[5]
                }
                for row in model_records
            ],
            "governance_history": [
                {
                    "ts_ms": row[0],
                    "action": row[1],
                    "decision": row[2],
                    "reason": row[3],
                    "metrics": json.loads(row[4] or "{}")
                }
                for row in governance_history
            ],
            "health_data": [
                {
                    "ts_ms": row[0],
                    "health_score": row[1],
                    "violation_type": row[2],
                    "violation_value": row[3],
                    "severity": row[4]
                }
                for row in health_data
            ],
            "rollback_history": [
                {
                    "ts_ms": row[0],
                    "strategy": row[1],
                    "reason": row[2],
                    "rollback_duration_ms": row[3],
                    "success": row[4],
                    "error_message": row[5]
                }
                for row in rollback_history
            ]
        }
        
    except Exception as e:
        return {"ok": False, "error": str(e)}

def api_evaluate_model_promotion(parsed=None, ctx=None):
    """Evaluate model for promotion"""
    try:
        model_name = parsed.get("model_name") if parsed else None
        model_kind = parsed.get("model_kind") if parsed else None
        model_ts_ms = parsed.get("model_ts_ms") if parsed else None
        regime = parsed.get("regime", "global") if parsed else "global"
        
        if not all([model_name, model_kind, model_ts_ms]):
            return {"ok": False, "error": "model_name, model_kind, and model_ts_ms required"}
        
        governance = get_governance()
        decision, result = governance.evaluate_promotion_readiness(
            model_name, model_kind, int(model_ts_ms), regime
        )
        
        return {
            "ok": True,
            "decision": decision.value,
            "evaluation": result,
            "timestamp": int(time.time() * 1000)
        }
        
    except Exception as e:
        return {"ok": False, "error": str(e)}

def api_manual_promotion(parsed=None, ctx=None):
    """Manually promote a model"""
    try:
        model_name = parsed.get("model_name") if parsed else None
        model_kind = parsed.get("model_kind") if parsed else None
        model_ts_ms = parsed.get("model_ts_ms") if parsed else None
        regime = parsed.get("regime", "global") if parsed else "global"
        reason = parsed.get("reason", "manual_operator_promotion") if parsed else "manual_operator_promotion"
        
        if not all([model_name, model_kind, model_ts_ms]):
            return {"ok": False, "error": "model_name, model_kind, and model_ts_ms required"}
        
        cc_manager = get_champion_challenger_manager()
        success, result = cc_manager.evaluate_challenger_promotion(
            model_name, model_kind, int(model_ts_ms), regime
        )
        
        if success:
            # Force promotion even if auto-promote is disabled
            from engine.strategy.model_registry import promote_to_champion
            from engine.strategy.promotion_audit import audit
            
            current_champion = None
            try:
                from engine.strategy.model_registry import get_stage_latest
                current_champion = get_stage_latest(model_name, "champion", regime=regime)
            except:
                pass
            
            promote_to_champion(model_name, model_kind, int(model_ts_ms), regime=regime)
            
            audit(
                actor="manual_operator",
                action="promote",
                model_name=model_name,
                from_kind=(current_champion["model_kind"] if current_champion else None),
                from_ts_ms=(current_champion["model_ts_ms"] if current_champion else None),
                to_kind=model_kind,
                to_ts_ms=int(model_ts_ms),
                reason={"manual": True, "operator_reason": reason},
                regime=regime
            )
        
        return {
            "ok": success,
            "result": result,
            "timestamp": int(time.time() * 1000)
        }
        
    except Exception as e:
        return {"ok": False, "error": str(e)}

def api_manual_rollback(parsed=None, ctx=None):
    """Manually rollback a model"""
    try:
        model_name = parsed.get("model_name") if parsed else None
        strategy = parsed.get("strategy", "immediate") if parsed else "immediate"
        reason = parsed.get("reason", "manual_operator_rollback") if parsed else "manual_operator_rollback"
        regime = parsed.get("regime", "global") if parsed else "global"
        
        if not model_name:
            return {"ok": False, "error": "model_name required"}
        
        # Parse strategy
        try:
            rollback_strategy = RollbackStrategy(strategy.lower())
        except ValueError:
            return {"ok": False, "error": f"Invalid strategy: {strategy}"}
        
        # Parse reason
        try:
            rollback_reason = RollbackReason(reason.lower())
        except ValueError:
            rollback_reason = RollbackReason.MANUAL_OPERATOR
        
        rollback_manager = get_rollback_manager()
        success, result = rollback_manager.execute_rollback(
            model_name, rollback_strategy, rollback_reason, regime, "manual_operator"
        )
        
        return {
            "ok": success,
            "result": result,
            "timestamp": int(time.time() * 1000)
        }
        
    except Exception as e:
        return {"ok": False, "error": str(e)}

def api_get_promotion_queue(parsed=None, ctx=None):
    """Get models waiting for promotion evaluation"""
    try:
        regime = parsed.get("regime", "global") if parsed else "global"
        
        con = db_connect()
        
        # Get challenger models
        challengers = con.execute(
            """
            SELECT mr.model_name, mr.model_kind, mr.model_ts_ms, mr.metrics_json,
                   mc.status, mc.created_ts_ms, mc.last_evaluated_ts_ms
            FROM model_registry mr
            LEFT JOIN model_competition mc ON (
                mr.model_name=mc.model_name AND 
                mr.model_kind=mc.model_kind AND 
                mr.model_ts_ms=mc.model_ts_ms
            )
            WHERE mr.stage='challenger' AND mr.regime=?
            ORDER BY mc.created_ts_ms DESC
            """,
            (regime,)
        ).fetchall()
        
        con.close()
        
        return {
            "ok": True,
            "regime": regime,
            "challengers": [
                {
                    "model_name": row[0],
                    "model_kind": row[1],
                    "model_ts_ms": row[2],
                    "metrics": json.loads(row[3] or "{}"),
                    "competition_status": row[4],
                    "created_ts_ms": row[5],
                    "last_evaluated_ts_ms": row[6]
                }
                for row in challengers
            ]
        }
        
    except Exception as e:
        return {"ok": False, "error": str(e)}

def api_get_system_alerts(parsed=None, ctx=None):
    """Get active system alerts and violations"""
    try:
        severity_filter = parsed.get("severity") if parsed else None
        limit = parsed.get("limit", 50) if parsed else 50
        
        con = db_connect()
        
        query = """
            SELECT ts_ms, model_name, violation_type, violation_value, 
                   threshold_value, severity
            FROM model_health_monitor
        """
        params = []
        
        if severity_filter:
            query += " WHERE severity=?"
            params.append(severity_filter.upper())
        
        query += " ORDER BY ts_ms DESC LIMIT ?"
        params.append(limit)
        
        alerts = con.execute(query, params).fetchall()
        
        # Get recent demotion history
        demotions = con.execute(
            """
            SELECT ts_ms, model_name, action, trigger_reason
            FROM demotion_history
            ORDER BY ts_ms DESC
            LIMIT 10
            """
        ).fetchall()
        
        con.close()
        
        return {
            "ok": True,
            "alerts": [
                {
                    "ts_ms": row[0],
                    "model_name": row[1],
                    "violation_type": row[2],
                    "violation_value": row[3],
                    "threshold_value": row[4],
                    "severity": row[5]
                }
                for row in alerts
            ],
            "recent_demotions": [
                {
                    "ts_ms": row[0],
                    "model_name": row[1],
                    "action": row[2],
                    "trigger_reason": row[3]
                }
                for row in demotions
            ]
        }
        
    except Exception as e:
        return {"ok": False, "error": str(e)}

def api_toggle_governance_feature(parsed=None, ctx=None):
    """Toggle governance features on/off"""
    try:
        feature = parsed.get("feature") if parsed else None
        enabled = parsed.get("enabled") if parsed else None
        
        if not feature or enabled is None:
            return {"ok": False, "error": "feature and enabled required"}
        
        feature = feature.lower()
        enabled = bool(enabled)
        
        # Update configuration based on feature
        if feature == "auto_promotion":
            cc_manager = get_champion_challenger_manager()
            cc_manager.config.auto_promote_enabled = enabled
        elif feature == "auto_demotion":
            demotion_manager = get_demotion_manager()
            demotion_manager.config.auto_rollback_enabled = enabled
        elif feature == "auto_rollback":
            rollback_manager = get_rollback_manager()
            rollback_manager.config.auto_rollback_enabled = enabled
        else:
            return {"ok": False, "error": f"Unknown feature: {feature}"}
        
        return {
            "ok": True,
            "feature": feature,
            "enabled": enabled,
            "timestamp": int(time.time() * 1000)
        }
        
    except Exception as e:
        return {"ok": False, "error": str(e)}
