"""
Alert Interaction Tracker

Tracks human operator interactions with alerts to learn relevance patterns
and improve signal-to-noise ratio through adaptive thresholding.
"""

import json
import time
import logging
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timedelta
from collections import defaultdict, deque
import sqlite3

from engine.storage import connect

logger = logging.getLogger(__name__)

class InteractionType(Enum):
    """Types of user interactions with alerts"""
    CLICK = "click"              # User clicked alert for details
    ACKNOWLEDGE = "acknowledge"  # User marked alert as seen
    IGNORE = "ignore"            # User dismissed without action
    SNOOZE = "snooze"            # User delayed alert
    ACTION = "action"            # User took action based on alert
    ESCALATE = "escalate"        # User escalated alert
    FALSE_POSITIVE = "false_positive"  # User marked as false positive

class AlertOutcome(Enum):
    """Outcome of alert after resolution"""
    TRUE_POSITIVE = "true_positive"
    FALSE_POSITIVE = "false_positive"
    BENIGN = "benign"
    ACTIONABLE = "actionable"
    IGNORED = "ignored"

@dataclass
class AlertInteraction:
    """Single interaction event with an alert"""
    alert_id: int
    interaction_type: InteractionType
    timestamp_ms: int
    operator_id: Optional[str] = None
    session_id: Optional[str] = None
    time_to_interaction_ms: Optional[int] = None  # Time from alert creation to interaction
    context: Dict[str, Any] = field(default_factory=dict)
    
@dataclass
class AlertLifecycle:
    """Complete lifecycle of an alert from creation to resolution"""
    alert_id: int
    created_ms: int
    resolved_ms: Optional[int] = None
    interactions: List[AlertInteraction] = field(default_factory=list)
    outcome: Optional[AlertOutcome] = None
    final_severity: Optional[str] = None
    operator_actions: List[str] = field(default_factory=list)

class AlertInteractionTracker:
    """Tracks and analyzes alert interactions for learning patterns"""
    
    def __init__(self, window_size_days: int = 30):
        self.window_size_days = window_size_days
        self.interaction_history: deque = deque(maxlen=10000)
        self.alert_lifecycles: Dict[int, AlertLifecycle] = {}
        
        # Learning parameters
        self.min_interactions_for_learning = 5
        self.significance_threshold = 0.05
        
        # Initialize database tables
        self._init_tables()
        
        logger.info("AlertInteractionTracker initialized")
    
    def _init_tables(self):
        """Initialize database tables for interaction tracking"""
        con = connect()
        try:
            # Alert interactions table
            con.execute("""
                CREATE TABLE IF NOT EXISTS alert_interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id INTEGER NOT NULL,
                    interaction_type TEXT NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    operator_id TEXT,
                    session_id TEXT,
                    time_to_interaction_ms INTEGER,
                    context_json TEXT,
                    FOREIGN KEY (alert_id) REFERENCES alerts (id)
                )
            """)
            
            # Alert lifecycle table
            con.execute("""
                CREATE TABLE IF NOT EXISTS alert_lifecycles (
                    alert_id INTEGER PRIMARY KEY,
                    created_ms INTEGER NOT NULL,
                    resolved_ms INTEGER,
                    outcome TEXT,
                    final_severity TEXT,
                    operator_actions_json TEXT,
                    FOREIGN KEY (alert_id) REFERENCES alerts (id)
                )
            """)
            
            # Interaction statistics for learning
            con.execute("""
                CREATE TABLE IF NOT EXISTS alert_interaction_stats (
                    rule_id TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    horizon_s INTEGER NOT NULL,
                    total_alerts INTEGER DEFAULT 0,
                    click_rate REAL DEFAULT 0.0,
                    acknowledge_rate REAL DEFAULT 0.0,
                    ignore_rate REAL DEFAULT 0.0,
                    action_rate REAL DEFAULT 0.0,
                    false_positive_rate REAL DEFAULT 0.0,
                    avg_time_to_interaction_ms REAL DEFAULT 0.0,
                    relevance_score REAL DEFAULT 0.5,
                    last_updated_ms INTEGER NOT NULL,
                    PRIMARY KEY (rule_id, severity, symbol, horizon_s)
                )
            """)
            
            # Create indexes
            con.execute("CREATE INDEX IF NOT EXISTS idx_alert_interactions_alert_id ON alert_interactions(alert_id)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_alert_interactions_timestamp ON alert_interactions(timestamp_ms)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_alert_interactions_type ON alert_interactions(interaction_type)")
            
            con.commit()
        finally:
            con.close()
    
    def track_interaction(self, alert_id: int, interaction_type: InteractionType,
                         operator_id: Optional[str] = None, session_id: Optional[str] = None,
                         context: Optional[Dict[str, Any]] = None) -> bool:
        """Track a user interaction with an alert"""
        try:
            now_ms = int(time.time() * 1000)
            
            # Get alert creation time to calculate time to interaction
            alert_created_ms = self._get_alert_created_ms(alert_id)
            time_to_interaction = now_ms - alert_created_ms if alert_created_ms else None
            
            # Create interaction record
            interaction = AlertInteraction(
                alert_id=alert_id,
                interaction_type=interaction_type,
                timestamp_ms=now_ms,
                operator_id=operator_id,
                session_id=session_id,
                time_to_interaction_ms=time_to_interaction,
                context=context or {}
            )
            
            # Store in database
            con = connect()
            try:
                con.execute("""
                    INSERT INTO alert_interactions 
                    (alert_id, interaction_type, timestamp_ms, operator_id, session_id, 
                     time_to_interaction_ms, context_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    alert_id, interaction_type.value, now_ms, operator_id, session_id,
                    time_to_interaction, json.dumps(context or {}, separators=(",", ":"))
                ))
                con.commit()
            finally:
                con.close()
            
            # Update lifecycle
            self._update_alert_lifecycle(alert_id, interaction)
            
            # Update learning statistics
            self._update_interaction_stats(alert_id, interaction_type)
            
            # Add to in-memory history
            self.interaction_history.append(interaction)
            
            logger.info(f"Tracked {interaction_type.value} interaction for alert {alert_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to track interaction for alert {alert_id}: {e}")
            return False
    
    def _get_alert_created_ms(self, alert_id: int) -> Optional[int]:
        """Get alert creation timestamp"""
        con = connect()
        try:
            row = con.execute(
                "SELECT ts_ms FROM alerts WHERE id = ?",
                (alert_id,)
            ).fetchone()
            return row[0] if row else None
        finally:
            con.close()
    
    def _update_alert_lifecycle(self, alert_id: int, interaction: AlertInteraction):
        """Update alert lifecycle with new interaction"""
        if alert_id not in self.alert_lifecycles:
            # Load existing lifecycle or create new
            self.alert_lifecycles[alert_id] = self._load_alert_lifecycle(alert_id)
        
        lifecycle = self.alert_lifecycles[alert_id]
        lifecycle.interactions.append(interaction)
        
        # Update outcome based on interaction
        if interaction.interaction_type == InteractionType.FALSE_POSITIVE:
            lifecycle.outcome = AlertOutcome.FALSE_POSITIVE
        elif interaction.interaction_type == InteractionType.ACTION:
            lifecycle.outcome = AlertOutcome.ACTIONABLE
    
    def _load_alert_lifecycle(self, alert_id: int) -> AlertLifecycle:
        """Load alert lifecycle from database"""
        con = connect()
        try:
            # Get alert info
            alert_row = con.execute(
                "SELECT ts_ms FROM alerts WHERE id = ?",
                (alert_id,)
            ).fetchone()
            
            if not alert_row:
                return AlertLifecycle(alert_id=alert_id, created_ms=int(time.time() * 1000))
            
            # Get lifecycle info
            lifecycle_row = con.execute(
                "SELECT * FROM alert_lifecycles WHERE alert_id = ?",
                (alert_id,)
            ).fetchone()
            
            if lifecycle_row:
                return AlertLifecycle(
                    alert_id=alert_id,
                    created_ms=alert_row[1],
                    resolved_ms=lifecycle_row[2],
                    outcome=AlertOutcome(lifecycle_row[3]) if lifecycle_row[3] else None,
                    final_severity=lifecycle_row[4],
                    operator_actions=json.loads(lifecycle_row[5] or "[]")
                )
            else:
                return AlertLifecycle(alert_id=alert_id, created_ms=alert_row[0])
                
        finally:
            con.close()
    
    def _update_interaction_stats(self, alert_id: int, interaction_type: InteractionType):
        """Update interaction statistics for learning"""
        try:
            # Get alert metadata
            alert_meta = self._get_alert_metadata(alert_id)
            if not alert_meta:
                return
            
            rule_id = alert_meta['rule_id']
            severity = alert_meta['severity']
            symbol = alert_meta['symbol']
            horizon_s = alert_meta['horizon_s']
            
            con = connect()
            try:
                # Get current stats
                row = con.execute("""
                    SELECT * FROM alert_interaction_stats 
                    WHERE rule_id = ? AND severity = ? AND symbol = ? AND horizon_s = ?
                """, (rule_id, severity, symbol, horizon_s)).fetchone()
                
                now_ms = int(time.time() * 1000)
                
                if row:
                    # Update existing stats
                    stats = list(row)
                    stats[4] += 1  # total_alerts
                    
                    # Update interaction rates
                    if interaction_type == InteractionType.CLICK:
                        stats[5] = self._update_rate(stats[5], stats[4], 1)  # click_rate
                    elif interaction_type == InteractionType.ACKNOWLEDGE:
                        stats[6] = self._update_rate(stats[6], stats[4], 1)  # acknowledge_rate
                    elif interaction_type == InteractionType.IGNORE:
                        stats[7] = self._update_rate(stats[7], stats[4], 1)  # ignore_rate
                    elif interaction_type == InteractionType.ACTION:
                        stats[8] = self._update_rate(stats[8], stats[4], 1)  # action_rate
                    elif interaction_type == InteractionType.FALSE_POSITIVE:
                        stats[9] = self._update_rate(stats[9], stats[4], 1)  # false_positive_rate
                    
                    # Update relevance score
                    stats[11] = self._calculate_relevance_score(stats[5], stats[6], stats[7], stats[8], stats[9])
                    stats[12] = now_ms  # last_updated_ms
                    
                    con.execute("""
                        UPDATE alert_interaction_stats 
                        SET total_alerts=?, click_rate=?, acknowledge_rate=?, ignore_rate=?,
                            action_rate=?, false_positive_rate=?, relevance_score=?, last_updated_ms=?
                        WHERE rule_id = ? AND severity = ? AND symbol = ? AND horizon_s = ?
                    """, (stats[4], stats[5], stats[6], stats[7], stats[8], stats[9], 
                          stats[11], stats[12], rule_id, severity, symbol, horizon_s))
                else:
                    # Create new stats
                    click_rate = 1.0 if interaction_type == InteractionType.CLICK else 0.0
                    acknowledge_rate = 1.0 if interaction_type == InteractionType.ACKNOWLEDGE else 0.0
                    ignore_rate = 1.0 if interaction_type == InteractionType.IGNORE else 0.0
                    action_rate = 1.0 if interaction_type == InteractionType.ACTION else 0.0
                    false_positive_rate = 1.0 if interaction_type == InteractionType.FALSE_POSITIVE else 0.0
                    
                    relevance_score = self._calculate_relevance_score(
                        click_rate, acknowledge_rate, ignore_rate, action_rate, false_positive_rate
                    )
                    
                    con.execute("""
                        INSERT INTO alert_interaction_stats 
                        (rule_id, severity, symbol, horizon_s, total_alerts, click_rate, 
                         acknowledge_rate, ignore_rate, action_rate, false_positive_rate, 
                         relevance_score, last_updated_ms)
                        VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                    """, (rule_id, severity, symbol, horizon_s, click_rate, acknowledge_rate,
                          ignore_rate, action_rate, false_positive_rate, relevance_score, now_ms))
                
                con.commit()
            finally:
                con.close()
                
        except Exception as e:
            logger.error(f"Failed to update interaction stats: {e}")
    
    def _get_alert_metadata(self, alert_id: int) -> Optional[Dict[str, Any]]:
        """Get alert metadata for statistics"""
        con = connect()
        try:
            row = con.execute("""
                SELECT rule_id, severity, symbol, horizon_s 
                FROM alerts WHERE id = ?
            """, (alert_id,)).fetchone()
            
            if row:
                return {
                    'rule_id': row[0],
                    'severity': row[1],
                    'symbol': row[2],
                    'horizon_s': row[3]
                }
            return None
        finally:
            con.close()
    
    def _update_rate(self, current_rate: float, total_count: int, new_count: int) -> float:
        """Update rate with new observations"""
        if total_count <= 1:
            return float(new_count) / max(1, total_count)
        return (current_rate * (total_count - 1) + new_count) / total_count
    
    def _calculate_relevance_score(self, click_rate: float, acknowledge_rate: float,
                                 ignore_rate: float, action_rate: float, false_positive_rate: float) -> float:
        """Calculate relevance score based on interaction patterns"""
        # Positive signals
        positive_score = (click_rate * 0.3 + acknowledge_rate * 0.2 + action_rate * 0.5)
        
        # Negative signals
        negative_score = (ignore_rate * 0.4 + false_positive_rate * 0.6)
        
        # Normalize to [0, 1]
        relevance = positive_score - negative_score
        return max(0.0, min(1.0, relevance))
    
    def get_relevance_score(self, rule_id: str, severity: str, symbol: str, horizon_s: int) -> float:
        """Get relevance score for alert configuration"""
        con = connect()
        try:
            row = con.execute("""
                SELECT relevance_score FROM alert_interaction_stats 
                WHERE rule_id = ? AND severity = ? AND symbol = ? AND horizon_s = ?
            """, (rule_id, severity, symbol, horizon_s)).fetchone()
            
            return row[0] if row and row[0] is not None else 0.5  # Default neutral score
        finally:
            con.close()
    
    def get_interaction_patterns(self, rule_id: Optional[str] = None, 
                               days_back: int = 7) -> Dict[str, Any]:
        """Get interaction patterns for analysis"""
        cutoff_ms = int((datetime.now() - timedelta(days=days_back)).timestamp() * 1000)
        
        con = connect()
        try:
            query = """
                SELECT interaction_type, COUNT(*) as count, AVG(time_to_interaction_ms) as avg_time
                FROM alert_interactions 
                WHERE timestamp_ms >= ?
            """
            params = [cutoff_ms]
            
            if rule_id:
                query += " AND alert_id IN (SELECT id FROM alerts WHERE rule_id = ?)"
                params.append(rule_id)
            
            query += " GROUP BY interaction_type"
            
            rows = con.execute(query, params).fetchall()
            
            patterns = {}
            for row in rows:
                interaction_type, count, avg_time = row
                patterns[interaction_type] = {
                    'count': count,
                    'avg_time_to_interaction_ms': avg_time
                }
            
            return patterns
        finally:
            con.close()
    
    def get_low_relevance_rules(self, min_alerts: int = 10, relevance_threshold: float = 0.3) -> List[Dict[str, Any]]:
        """Get rules with consistently low relevance scores"""
        con = connect()
        try:
            rows = con.execute("""
                SELECT rule_id, severity, symbol, horizon_s, total_alerts, relevance_score,
                       click_rate, ignore_rate, false_positive_rate
                FROM alert_interaction_stats 
                WHERE total_alerts >= ? AND relevance_score < ?
                ORDER BY relevance_score ASC
            """, (min_alerts, relevance_threshold)).fetchall()
            
            results = []
            for row in rows:
                results.append({
                    'rule_id': row[0],
                    'severity': row[1],
                    'symbol': row[2],
                    'horizon_s': row[3],
                    'total_alerts': row[4],
                    'relevance_score': row[5],
                    'click_rate': row[6],
                    'ignore_rate': row[7],
                    'false_positive_rate': row[8]
                })
            
            return results
        finally:
            con.close()
    
    def cleanup_old_data(self, days_to_keep: int = 90):
        """Clean up old interaction data"""
        cutoff_ms = int((datetime.now() - timedelta(days=days_to_keep)).timestamp() * 1000)
        
        con = connect()
        try:
            # Clean old interactions
            con.execute("DELETE FROM alert_interactions WHERE timestamp_ms < ?", (cutoff_ms,))
            
            # Clean old lifecycles for resolved alerts
            con.execute("""
                DELETE FROM alert_lifecycles 
                WHERE resolved_ms IS NOT NULL AND resolved_ms < ?
            """, (cutoff_ms,))
            
            con.commit()
            logger.info(f"Cleaned interaction data older than {days_to_keep} days")
        finally:
            con.close()

# Global tracker instance
interaction_tracker = AlertInteractionTracker()
