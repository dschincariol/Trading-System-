# engine/execution/execution_ai_advisor.py
"""
Execution AI Advisor - Safe Integration Layer

Provides AI recommendations to broker adapters in advisory mode only.
No direct execution authority - all recommendations require explicit approval.
"""

import json
import time
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from enum import Enum

from engine.storage import connect
from engine.execution.execution_ai_features import ExecutionFeatures, extract_execution_features, store_execution_features
from engine.execution.execution_ai_model import (
    ExecutionRecommendation, 
    get_execution_recommendation, 
    is_model_trained,
    get_model_metrics
)


class AdvisoryStatus(Enum):
    """Status of AI advisory recommendations"""
    PENDING = "pending"
    APPROVED = "approved" 
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"


@dataclass
class AdvisoryRecommendation:
    """AI advisory recommendation record"""
    recommendation_id: str
    client_order_id: str
    broker: str
    symbol: str
    action: str
    confidence: float
    reasoning: str
    
    # Specific parameters
    slice_size: Optional[float] = None
    delay_ms: Optional[int] = None
    limit_offset_bps: Optional[float] = None
    urgency_multiplier: Optional[float] = None
    
    # Expected outcomes
    expected_slippage_bps: Optional[float] = None
    fill_probability: Optional[float] = None
    cost_efficiency: Optional[float] = None
    
    # Metadata
    created_ts_ms: int = 0
    expires_ts_ms: int = 0
    status: AdvisoryStatus = AdvisoryStatus.PENDING
    approved_by: Optional[str] = None
    approval_ts_ms: Optional[int] = None
    execution_outcome: Optional[Dict[str, Any]] = None


class ExecutionAIAdvisor:
    """Main AI advisor class - advisory mode only"""
    
    def __init__(self, advisory_ttl_ms: int = 60000):  # 1 minute default
        self.advisory_ttl_ms = advisory_ttl_ms
        
    def _ensure_advisory_tables(self, con) -> None:
        """Create tables for advisory recommendations"""
        con.executescript("""
        CREATE TABLE IF NOT EXISTS execution_ai_advisory (
            recommendation_id TEXT PRIMARY KEY,
            client_order_id TEXT NOT NULL,
            broker TEXT NOT NULL,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            confidence REAL NOT NULL,
            reasoning TEXT NOT NULL,
            
            slice_size REAL,
            delay_ms INTEGER,
            limit_offset_bps REAL,
            urgency_multiplier REAL,
            
            expected_slippage_bps REAL,
            fill_probability REAL,
            cost_efficiency REAL,
            
            created_ts_ms INTEGER NOT NULL,
            expires_ts_ms INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            approved_by TEXT,
            approval_ts_ms INTEGER,
            execution_outcome_json TEXT,
            
            features_json TEXT,
            model_version TEXT
        );
        
        CREATE INDEX IF NOT EXISTS idx_ai_advisory_client_order ON execution_ai_advisory(client_order_id);
        CREATE INDEX IF NOT EXISTS idx_ai_advisory_status ON execution_ai_advisory(status);
        CREATE INDEX IF NOT EXISTS idx_ai_advisory_created ON execution_ai_advisory(created_ts_ms);
        
        -- Advisory audit log
        CREATE TABLE IF NOT EXISTS execution_ai_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER NOT NULL,
            recommendation_id TEXT,
            client_order_id TEXT,
            action TEXT,
            status_before TEXT,
            status_after TEXT,
            changed_by TEXT,
            notes TEXT
        );
        
        CREATE INDEX IF NOT EXISTS idx_ai_audit_ts ON execution_ai_audit_log(ts_ms);
        """)
    
    def generate_advisory_recommendation(
        self,
        client_order_id: str,
        broker: str,
        symbol: str,
        qty: float,
        aggressiveness: str,
        alpha_ttl_ms: int = 0,
        alpha_half_life_ms: int = 60000,
        venue: Optional[str] = None,
        instrument_type: Optional[str] = None,
        submit_ts_ms: Optional[int] = None
    ) -> Optional[AdvisoryRecommendation]:
        """Generate AI advisory recommendation for an order"""
        
        if not is_model_trained():
            return None
        
        # Extract features
        features = extract_execution_features(
            client_order_id=client_order_id,
            broker=broker,
            symbol=symbol,
            qty=qty,
            aggressiveness=aggressiveness,
            alpha_ttl_ms=alpha_ttl_ms,
            alpha_half_life_ms=alpha_half_life_ms,
            venue=venue,
            instrument_type=instrument_type,
            submit_ts_ms=submit_ts_ms
        )
        
        # Store features for training
        store_execution_features(features, client_order_id)
        
        # Get current spread estimate
        current_spread_bps = self._estimate_current_spread(symbol)
        
        # Get AI recommendation
        ai_recommendation = get_execution_recommendation(
            features, current_spread_bps
        )
        
        # Create advisory recommendation
        now_ms = int(time.time() * 1000)
        recommendation_id = f"ai_adv_{client_order_id}_{int(now_ms)}"
        
        advisory = AdvisoryRecommendation(
            recommendation_id=recommendation_id,
            client_order_id=client_order_id,
            broker=broker,
            symbol=symbol,
            action=ai_recommendation.action.value,
            confidence=ai_recommendation.confidence,
            reasoning=ai_recommendation.reasoning,
            slice_size=ai_recommendation.slice_size,
            delay_ms=ai_recommendation.delay_ms,
            limit_offset_bps=ai_recommendation.limit_offset_bps,
            urgency_multiplier=ai_recommendation.urgency_multiplier,
            expected_slippage_bps=ai_recommendation.expected_slippage_bps,
            fill_probability=ai_recommendation.fill_probability,
            cost_efficiency=ai_recommendation.cost_efficiency,
            created_ts_ms=now_ms,
            expires_ts_ms=now_ms + self.advisory_ttl_ms,
            status=AdvisoryStatus.PENDING
        )
        
        # Store advisory
        self._store_advisory(advisory, features)
        
        return advisory
    
    def _estimate_current_spread(self, symbol: str) -> float:
        """Estimate current spread for a symbol"""
        con = connect()
        try:
            row = con.execute("""
                SELECT AVG(spread_bps)
                FROM execution_analytics
                WHERE symbol = ? AND ts_ms > ?
                LIMIT 10
            """, (symbol, time.time() * 1000 - 300_000)).fetchone()  # 5 min lookback
            
            if row and row[0]:
                return float(row[0])
            
            # Fallback to recent fills
            row = con.execute("""
                SELECT AVG(CASE WHEN side = 'BUY' THEN fill_px ELSE -fill_px END) as avg_price,
                       COUNT(*) as fill_count
                FROM execution_fills f
                WHERE symbol = ? AND fill_ts_ms > ?
                LIMIT 20
            """, (symbol, time.time() * 1000 - 300_000)).fetchone()
            
            # Default spread if no data
            return 2.5
            
        finally:
            con.close()
    
    def _store_advisory(self, advisory: AdvisoryRecommendation, features: ExecutionFeatures) -> None:
        """Store advisory recommendation"""
        con = connect()
        try:
            self._ensure_advisory_tables(con)
            
            con.execute("""
                INSERT INTO execution_ai_advisory(
                    recommendation_id, client_order_id, broker, symbol, action, confidence, reasoning,
                    slice_size, delay_ms, limit_offset_bps, urgency_multiplier,
                    expected_slippage_bps, fill_probability, cost_efficiency,
                    created_ts_ms, expires_ts_ms, status,
                    features_json, model_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                advisory.recommendation_id,
                advisory.client_order_id,
                advisory.broker,
                advisory.symbol,
                advisory.action,
                advisory.confidence,
                advisory.reasoning,
                advisory.slice_size,
                advisory.delay_ms,
                advisory.limit_offset_bps,
                advisory.urgency_multiplier,
                advisory.expected_slippage_bps,
                advisory.fill_probability,
                advisory.cost_efficiency,
                advisory.created_ts_ms,
                advisory.expires_ts_ms,
                advisory.status.value,
                json.dumps(asdict(features), separators=(',', ':'), sort_keys=True),
                "v1.0"  # will be updated with actual model version
            ))
            
            con.commit()
            
        finally:
            con.close()
    
    def get_pending_recommendations(self, broker: Optional[str] = None) -> List[AdvisoryRecommendation]:
        """Get pending advisory recommendations"""
        con = connect()
        try:
            self._ensure_advisory_tables(con)
            
            query = """
                SELECT recommendation_id, client_order_id, broker, symbol, action, confidence, reasoning,
                       slice_size, delay_ms, limit_offset_bps, urgency_multiplier,
                       expected_slippage_bps, fill_probability, cost_efficiency,
                       created_ts_ms, expires_ts_ms, status, approved_by, approval_ts_ms,
                       execution_outcome_json
                FROM execution_ai_advisory
                WHERE status = 'pending' AND expires_ts_ms > ?
            """
            params = [int(time.time() * 1000)]
            
            if broker:
                query += " AND broker = ?"
                params.append(broker)
            
            query += " ORDER BY created_ts_ms DESC"
            
            rows = con.execute(query, params).fetchall()
            
            recommendations = []
            for row in rows:
                execution_outcome = None
                if row[19]:
                    try:
                        execution_outcome = json.loads(row[19])
                    except Exception:
                        pass
                
                recommendations.append(AdvisoryRecommendation(
                    recommendation_id=row[0],
                    client_order_id=row[1],
                    broker=row[2],
                    symbol=row[3],
                    action=row[4],
                    confidence=float(row[5]),
                    reasoning=row[6],
                    slice_size=float(row[7]) if row[7] else None,
                    delay_ms=int(row[8]) if row[8] else None,
                    limit_offset_bps=float(row[9]) if row[9] else None,
                    urgency_multiplier=float(row[10]) if row[10] else None,
                    expected_slippage_bps=float(row[11]) if row[11] else None,
                    fill_probability=float(row[12]) if row[12] else None,
                    cost_efficiency=float(row[13]) if row[13] else None,
                    created_ts_ms=int(row[14]),
                    expires_ts_ms=int(row[15]),
                    status=AdvisoryStatus(row[16]),
                    approved_by=row[17],
                    approval_ts_ms=int(row[18]) if row[18] else None,
                    execution_outcome=execution_outcome
                ))
            
            return recommendations
            
        finally:
            con.close()
    
    def approve_recommendation(
        self,
        recommendation_id: str,
        approved_by: str,
        notes: Optional[str] = None
    ) -> bool:
        """Approve an advisory recommendation"""
        con = connect()
        try:
            self._ensure_advisory_tables(con)
            
            now_ms = int(time.time() * 1000)
            
            # Update recommendation status
            cursor = con.execute("""
                UPDATE execution_ai_advisory
                SET status = 'approved', approved_by = ?, approval_ts_ms = ?
                WHERE recommendation_id = ? AND status = 'pending' AND expires_ts_ms > ?
            """, (approved_by, now_ms, recommendation_id, now_ms))
            
            if cursor.rowcount == 0:
                return False
            
            # Log approval
            con.execute("""
                INSERT INTO execution_ai_audit_log(
                    ts_ms, recommendation_id, action, status_before, status_after, changed_by, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (now_ms, recommendation_id, 'approve', 'pending', 'approved', approved_by, notes))
            
            con.commit()
            return True
            
        finally:
            con.close()
    
    def reject_recommendation(
        self,
        recommendation_id: str,
        rejected_by: str,
        reason: Optional[str] = None
    ) -> bool:
        """Reject an advisory recommendation"""
        con = connect()
        try:
            self._ensure_advisory_tables(con)
            
            now_ms = int(time.time() * 1000)
            
            # Update recommendation status
            cursor = con.execute("""
                UPDATE execution_ai_advisory
                SET status = 'rejected'
                WHERE recommendation_id = ? AND status = 'pending' AND expires_ts_ms > ?
            """, (recommendation_id, now_ms))
            
            if cursor.rowcount == 0:
                return False
            
            # Log rejection
            con.execute("""
                INSERT INTO execution_ai_audit_log(
                    ts_ms, recommendation_id, action, status_before, status_after, changed_by, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (now_ms, recommendation_id, 'reject', 'pending', 'rejected', rejected_by, reason))
            
            con.commit()
            return True
            
        finally:
            con.close()
    
    def record_execution_outcome(
        self,
        recommendation_id: str,
        actual_slippage_bps: float,
        actual_fill_latency_ms: float,
        actual_cost_bps: float,
        notes: Optional[str] = None
    ) -> bool:
        """Record actual execution outcome for learning"""
        con = connect()
        try:
            self._ensure_advisory_tables(con)
            
            now_ms = int(time.time() * 1000)
            
            outcome = {
                'actual_slippage_bps': actual_slippage_bps,
                'actual_fill_latency_ms': actual_fill_latency_ms,
                'actual_cost_bps': actual_cost_bps,
                'recorded_ts_ms': now_ms,
                'notes': notes
            }
            
            # Update recommendation with outcome
            cursor = con.execute("""
                UPDATE execution_ai_advisory
                SET status = 'executed', execution_outcome_json = ?
                WHERE recommendation_id = ? AND status = 'approved'
            """, (json.dumps(outcome, separators=(',', ':'), sort_keys=True), recommendation_id))
            
            if cursor.rowcount == 0:
                return False
            
            # Update features table with actual outcomes for training
            client_order_id = con.execute("""
                SELECT client_order_id FROM execution_ai_advisory WHERE recommendation_id = ?
            """, (recommendation_id,)).fetchone()[0]
            
            if client_order_id:
                con.execute("""
                    UPDATE execution_features
                    SET actual_slippage_bps = ?, actual_fill_latency_ms = ?, actual_cost_bps = ?
                    WHERE client_order_id = ?
                """, (actual_slippage_bps, actual_fill_latency_ms, actual_cost_bps, client_order_id))
            
            # Log outcome
            con.execute("""
                INSERT INTO execution_ai_audit_log(
                    ts_ms, recommendation_id, action, status_before, status_after, changed_by, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (now_ms, recommendation_id, 'outcome', 'approved', 'executed', 'system', 
                   json.dumps(outcome, separators=(',', ':'), sort_keys=True)))
            
            con.commit()
            return True
            
        finally:
            con.close()
    
    def expire_old_recommendations(self) -> int:
        """Expire old pending recommendations"""
        con = connect()
        try:
            self._ensure_advisory_tables(con)
            
            now_ms = int(time.time() * 1000)
            
            cursor = con.execute("""
                UPDATE execution_ai_advisory
                SET status = 'expired'
                WHERE status = 'pending' AND expires_ts_ms <= ?
            """, (now_ms,))
            
            expired_count = cursor.rowcount
            
            if expired_count > 0:
                # Log expirations
                con.execute("""
                    INSERT INTO execution_ai_audit_log(ts_ms, action, status_before, status_after, changed_by, notes)
                    VALUES (?, 'expire', 'pending', 'expired', 'system', ?)
                """, (now_ms, f'Expired {expired_count} recommendations'))
            
            con.commit()
            return expired_count
            
        finally:
            con.close()
    
    def get_advisory_stats(self, days: int = 7) -> Dict[str, Any]:
        """Get advisory performance statistics"""
        con = connect()
        try:
            self._ensure_advisory_tables(con)
            
            since_ts = int(time.time() * 1000) - (days * 24 * 3600 * 1000)
            
            # Overall stats
            stats_row = con.execute("""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) as approved,
                    SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) as rejected,
                    SUM(CASE WHEN status = 'expired' THEN 1 ELSE 0 END) as expired,
                    SUM(CASE WHEN status = 'executed' THEN 1 ELSE 0 END) as executed,
                    AVG(confidence) as avg_confidence
                FROM execution_ai_advisory
                WHERE created_ts_ms > ?
            """, (since_ts,)).fetchone()
            
            if not stats_row or stats_row[0] == 0:
                return {'ok': False, 'message': 'No advisory data found'}
            
            # Action breakdown
            action_rows = con.execute("""
                SELECT action, COUNT(*) as count, AVG(confidence) as avg_conf
                FROM execution_ai_advisory
                WHERE created_ts_ms > ?
                GROUP BY action
                ORDER BY count DESC
            """, (since_ts,)).fetchall()
            
            # Performance stats for executed recommendations
            perf_rows = con.execute("""
                SELECT 
                    AVG(CAST(JSON_EXTRACT(execution_outcome_json, '$.actual_slippage_bps') AS REAL)) as avg_actual_slip,
                    AVG(CAST(JSON_EXTRACT(execution_outcome_json, '$.actual_cost_bps') AS REAL)) as avg_actual_cost,
                    AVG(expected_slippage_bps) as avg_expected_slip,
                    AVG(cost_efficiency) as avg_efficiency
                FROM execution_ai_advisory
                WHERE created_ts_ms > ? AND status = 'executed'
            """, (since_ts,)).fetchone()
            
            return {
                'ok': True,
                'period_days': days,
                'total_recommendations': int(stats_row[0]),
                'approved': int(stats_row[1]),
                'rejected': int(stats_row[2]),
                'expired': int(stats_row[3]),
                'executed': int(stats_row[4]),
                'approval_rate': float(stats_row[1]) / max(1, int(stats_row[0])),
                'avg_confidence': float(stats_row[5] or 0),
                'action_breakdown': [
                    {'action': row[0], 'count': int(row[1]), 'avg_confidence': float(row[2] or 0)}
                    for row in action_rows
                ],
                'performance': {
                    'avg_actual_slippage_bps': float(perf_rows[0] or 0),
                    'avg_actual_cost_bps': float(perf_rows[1] or 0),
                    'avg_expected_slippage_bps': float(perf_rows[2] or 0),
                    'avg_cost_efficiency': float(perf_rows[3] or 0),
                    'slippage_prediction_error': abs(float(perf_rows[0] or 0) - float(perf_rows[2] or 0))
                } if perf_rows[0] else None
            }
            
        finally:
            con.close()


# Global advisor instance
_execution_ai_advisor = None


def get_execution_ai_advisor() -> ExecutionAIAdvisor:
    """Get or create the global execution AI advisor"""
    global _execution_ai_advisor
    if _execution_ai_advisor is None:
        _execution_ai_advisor = ExecutionAIAdvisor()
    return _execution_ai_advisor


def request_ai_advisory(
    client_order_id: str,
    broker: str,
    symbol: str,
    qty: float,
    aggressiveness: str,
    alpha_ttl_ms: int = 0,
    alpha_half_life_ms: int = 60000,
    venue: Optional[str] = None,
    instrument_type: Optional[str] = None,
    submit_ts_ms: Optional[int] = None
) -> Optional[AdvisoryRecommendation]:
    """Request AI advisory recommendation for an order"""
    advisor = get_execution_ai_advisor()
    return advisor.generate_advisory_recommendation(
        client_order_id, broker, symbol, qty, aggressiveness,
        alpha_ttl_ms, alpha_half_life_ms, venue, instrument_type, submit_ts_ms
    )


def get_pending_ai_recommendations(broker: Optional[str] = None) -> List[AdvisoryRecommendation]:
    """Get pending AI recommendations"""
    advisor = get_execution_ai_advisor()
    return advisor.get_pending_recommendations(broker)


def approve_ai_recommendation(recommendation_id: str, approved_by: str, notes: Optional[str] = None) -> bool:
    """Approve an AI recommendation"""
    advisor = get_execution_ai_advisor()
    return advisor.approve_recommendation(recommendation_id, approved_by, notes)


def reject_ai_recommendation(recommendation_id: str, rejected_by: str, reason: Optional[str] = None) -> bool:
    """Reject an AI recommendation"""
    advisor = get_execution_ai_advisor()
    return advisor.reject_recommendation(recommendation_id, rejected_by, reason)


def record_ai_execution_outcome(
    recommendation_id: str,
    actual_slippage_bps: float,
    actual_fill_latency_ms: float,
    actual_cost_bps: float,
    notes: Optional[str] = None
) -> bool:
    """Record execution outcome for AI learning"""
    advisor = get_execution_ai_advisor()
    return advisor.record_execution_outcome(
        recommendation_id, actual_slippage_bps, actual_fill_latency_ms, actual_cost_bps, notes
    )
