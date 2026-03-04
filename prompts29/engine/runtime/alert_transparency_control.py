"""
Alert Transparency and Override Controls

Provides transparency into AI decisions and allows human operators
to override adaptive alerting behavior when needed.
"""

import json
import time
import logging
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timedelta
from collections import defaultdict

from engine.storage import connect
from engine.runtime.adaptive_alerting import adaptive_alerting, FilterReason
from engine.runtime.human_alignment_ai import human_alignment_ai, AdaptationAction

logger = logging.getLogger(__name__)

class OverrideType(Enum):
    """Types of overrides operators can apply"""
    DISABLE_ADAPTIVE = "disable_adaptive"          # Disable adaptive adjustments
    FORCE_ALERT = "force_alert"                    # Force alert through filters
    BLOCK_RULE = "block_rule"                      # Block specific rule
    ADJUST_THRESHOLD = "adjust_threshold"          # Manual threshold adjustment
    RESET_LEARNING = "reset_learning"              # Reset learning for rule
    EMERGENCY_MODE = "emergency_mode"              # Emergency bypass mode

class OverrideScope(Enum):
    """Scope of override"""
    SINGLE_ALERT = "single_alert"
    RULE_CONFIG = "rule_config"
    SYMBOL_SPECIFIC = "symbol_specific"
    GLOBAL = "global"

class OverrideDuration(Enum):
    """Duration of override"""
    TEMPORARY = "temporary"        # One-time
    SESSION = "session"           # Current session
    TIME_LIMITED = "time_limited" # Specific duration
    PERMANENT = "permanent"       # Until manually removed

@dataclass
class Override:
    """Operator override configuration"""
    override_id: str
    override_type: OverrideType
    scope: OverrideScope
    duration: OverrideDuration
    target: str  # rule_id, symbol, or "global"
    parameters: Dict[str, Any] = field(default_factory=dict)
    operator_id: str
    reason: str
    created_ms: int
    expires_ms: Optional[int] = None
    active: bool = True
    usage_count: int = 0

@dataclass
class TransparencyRecord:
    """Record of AI decision for transparency"""
    alert_id: Optional[int]
    decision_type: str
    decision: str
    reasoning: Dict[str, Any]
    confidence: float
    timestamp_ms: int
    operator_visible: bool = True

class AlertTransparencyControl:
    """Transparency and override control system"""
    
    def __init__(self):
        self.active_overrides: Dict[str, Override] = {}
        self.transparency_log: List[TransparencyRecord] = []
        self.session_overrides: List[str] = []  # Override IDs for current session
        
        # Initialize database tables
        self._init_tables()
        
        # Load existing overrides
        self._load_active_overrides()
        
        logger.info("AlertTransparencyControl initialized")
    
    def _init_tables(self):
        """Initialize database tables for transparency and overrides"""
        con = connect()
        try:
            # Overrides table
            con.execute("""
                CREATE TABLE IF NOT EXISTS alert_overrides (
                    override_id TEXT PRIMARY KEY,
                    override_type TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    duration TEXT NOT NULL,
                    target TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    operator_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_ms INTEGER NOT NULL,
                    expires_ms INTEGER,
                    active INTEGER DEFAULT 1,
                    usage_count INTEGER DEFAULT 0
                )
            """)
            
            # Transparency log table
            con.execute("""
                CREATE TABLE IF NOT EXISTS alert_transparency_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id INTEGER,
                    decision_type TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reasoning_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    operator_visible INTEGER DEFAULT 1
                )
            """)
            
            # Override audit log
            con.execute("""
                CREATE TABLE IF NOT EXISTS override_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    override_id TEXT NOT NULL,
                    action TEXT NOT NULL,  -- 'created', 'applied', 'expired', 'revoked'
                    operator_id TEXT NOT NULL,
                    context_json TEXT,
                    timestamp_ms INTEGER NOT NULL,
                    FOREIGN KEY (override_id) REFERENCES alert_overrides (override_id)
                )
            """)
            
            con.commit()
        finally:
            con.close()
    
    def _load_active_overrides(self):
        """Load active overrides from database"""
        con = connect()
        try:
            now_ms = int(time.time() * 1000)
            
            rows = con.execute("""
                SELECT * FROM alert_overrides 
                WHERE active = 1 AND (expires_ms IS NULL OR expires_ms > ?)
                ORDER BY created_ms DESC
            """, (now_ms,)).fetchall()
            
            for row in rows:
                override = Override(
                    override_id=row[0],
                    override_type=OverrideType(row[1]),
                    scope=OverrideScope(row[2]),
                    duration=OverrideDuration(row[3]),
                    target=row[4],
                    parameters=json.loads(row[5]),
                    operator_id=row[6],
                    reason=row[7],
                    created_ms=row[8],
                    expires_ms=row[9],
                    active=bool(row[10]),
                    usage_count=row[11]
                )
                
                self.active_overrides[override.override_id] = override
                
        except Exception as e:
            logger.error(f"Failed to load overrides: {e}")
        finally:
            con.close()
    
    def create_override(self, override_type: OverrideType, scope: OverrideScope,
                       duration: OverrideDuration, target: str, parameters: Dict[str, Any],
                       operator_id: str, reason: str, 
                       duration_hours: Optional[int] = None) -> str:
        """Create a new override"""
        try:
            override_id = f"override_{int(time.time())}_{len(self.active_overrides)}"
            
            now_ms = int(time.time() * 1000)
            expires_ms = None
            
            if duration == OverrideDuration.TIME_LIMITED and duration_hours:
                expires_ms = now_ms + (duration_hours * 3600 * 1000)
            elif duration == OverrideDuration.SESSION:
                self.session_overrides.append(override_id)
            
            override = Override(
                override_id=override_id,
                override_type=override_type,
                scope=scope,
                duration=duration,
                target=target,
                parameters=parameters,
                operator_id=operator_id,
                reason=reason,
                created_ms=now_ms,
                expires_ms=expires_ms
            )
            
            # Store in database
            con = connect()
            try:
                con.execute("""
                    INSERT INTO alert_overrides 
                    (override_id, override_type, scope, duration, target, parameters_json,
                     operator_id, reason, created_ms, expires_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    override_id, override_type.value, scope.value, duration.value,
                    target, json.dumps(parameters), operator_id, reason,
                    now_ms, expires_ms
                ))
                
                # Log to audit
                con.execute("""
                    INSERT INTO override_audit_log 
                    (override_id, action, operator_id, context_json, timestamp_ms)
                    VALUES (?, ?, ?, ?, ?)
                """, (override_id, 'created', operator_id, json.dumps({
                    'reason': reason,
                    'parameters': parameters
                }), now_ms))
                
                con.commit()
            finally:
                con.close()
            
            # Add to active overrides
            self.active_overrides[override_id] = override
            
            logger.info(f"Created override {override_id}: {override_type.value} by {operator_id}")
            return override_id
            
        except Exception as e:
            logger.error(f"Failed to create override: {e}")
            raise
    
    def apply_override_to_alert(self, alert_id: int, rule_id: str, severity: str,
                              symbol: str, horizon_s: int) -> Dict[str, Any]:
        """Check and apply relevant overrides to an alert"""
        override_effects = {
            'force_through': False,
            'block_alert': False,
            'disable_adaptive': False,
            'threshold_override': None,
            'applied_overrides': []
        }
        
        try:
            # Check for relevant overrides
            for override in self.active_overrides.values():
                if not override.active:
                    continue
                
                # Check if override applies to this alert
                if self._override_applies(override, rule_id, severity, symbol, horizon_s):
                    override_effects['applied_overrides'].append(override.override_id)
                    override.usage_count += 1
                    
                    # Apply override effects
                    if override.override_type == OverrideType.FORCE_ALERT:
                        override_effects['force_through'] = True
                    elif override.override_type == OverrideType.BLOCK_RULE:
                        override_effects['block_alert'] = True
                    elif override.override_type == OverrideType.DISABLE_ADAPTIVE:
                        override_effects['disable_adaptive'] = True
                    elif override.override_type == OverrideType.ADJUST_THRESHOLD:
                        override_effects['threshold_override'] = override.parameters.get('threshold')
                    elif override.override_type == OverrideType.EMERGENCY_MODE:
                        override_effects['force_through'] = True
                        override_effects['disable_adaptive'] = True
            
            # Log transparency record
            self._log_transparency(
                alert_id=alert_id,
                decision_type="override_check",
                decision="overrides_applied" if override_effects['applied_overrides'] else "no_overrides",
                reasoning=override_effects,
                confidence=1.0
            )
            
            return override_effects
            
        except Exception as e:
            logger.error(f"Failed to apply overrides to alert {alert_id}: {e}")
            return override_effects
    
    def _override_applies(self, override: Override, rule_id: str, severity: str,
                         symbol: str, horizon_s: int) -> bool:
        """Check if override applies to specific alert"""
        if override.scope == OverrideScope.GLOBAL:
            return override.target == "global"
        
        elif override.scope == OverrideScope.SINGLE_ALERT:
            # This would need specific alert ID matching
            return False
        
        elif override.scope == OverrideScope.RULE_CONFIG:
            return override.target == rule_id
        
        elif override.scope == OverrideScope.SYMBOL_SPECIFIC:
            return override.target == symbol
        
        return False
    
    def _log_transparency(self, alert_id: Optional[int], decision_type: str,
                         decision: str, reasoning: Dict[str, Any], confidence: float):
        """Log transparency record"""
        try:
            record = TransparencyRecord(
                alert_id=alert_id,
                decision_type=decision_type,
                decision=decision,
                reasoning=reasoning,
                confidence=confidence,
                timestamp_ms=int(time.time() * 1000)
            )
            
            self.transparency_log.append(record)
            
            # Store in database
            con = connect()
            try:
                con.execute("""
                    INSERT INTO alert_transparency_log 
                    (alert_id, decision_type, decision, reasoning_json, confidence, timestamp_ms)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    alert_id, decision_type, decision, json.dumps(reasoning),
                    confidence, record.timestamp_ms
                ))
                con.commit()
            finally:
                con.close()
                
        except Exception as e:
            logger.error(f"Failed to log transparency record: {e}")
    
    def log_ai_decision(self, alert_id: Optional[int], decision_type: str,
                       decision: str, reasoning: Dict[str, Any], confidence: float):
        """Log AI decision for transparency"""
        self._log_transparency(alert_id, decision_type, decision, reasoning, confidence)
    
    def revoke_override(self, override_id: str, operator_id: str, reason: str) -> bool:
        """Revoke an active override"""
        try:
            if override_id not in self.active_overrides:
                return False
            
            override = self.active_overrides[override_id]
            override.active = False
            
            # Update database
            con = connect()
            try:
                con.execute("UPDATE alert_overrides SET active = 0 WHERE override_id = ?", (override_id,))
                
                # Log to audit
                con.execute("""
                    INSERT INTO override_audit_log 
                    (override_id, action, operator_id, context_json, timestamp_ms)
                    VALUES (?, ?, ?, ?, ?)
                """, (override_id, 'revoked', operator_id, json.dumps({'reason': reason}), int(time.time() * 1000)))
                
                con.commit()
            finally:
                con.close()
            
            # Remove from active overrides
            del self.active_overrides[override_id]
            
            logger.info(f"Revoked override {override_id} by {operator_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to revoke override {override_id}: {e}")
            return False
    
    def get_transparency_report(self, alert_id: Optional[int] = None,
                              hours_back: int = 24) -> List[Dict[str, Any]]:
        """Get transparency report for alerts"""
        cutoff_ms = int((datetime.now() - timedelta(hours=hours_back)).timestamp() * 1000)
        
        con = connect()
        try:
            query = "SELECT * FROM alert_transparency_log WHERE timestamp_ms >= ?"
            params = [cutoff_ms]
            
            if alert_id:
                query += " AND alert_id = ?"
                params.append(alert_id)
            
            query += " ORDER BY timestamp_ms DESC"
            
            rows = con.execute(query, params).fetchall()
            
            report = []
            for row in rows:
                report.append({
                    'alert_id': row[1],
                    'decision_type': row[2],
                    'decision': row[3],
                    'reasoning': json.loads(row[4]),
                    'confidence': row[5],
                    'timestamp_ms': row[6],
                    'operator_visible': bool(row[7])
                })
            
            return report
            
        except Exception as e:
            logger.error(f"Failed to get transparency report: {e}")
            return []
        finally:
            con.close()
    
    def get_active_overrides_summary(self) -> List[Dict[str, Any]]:
        """Get summary of active overrides"""
        summary = []
        
        for override in self.active_overrides.values():
            summary.append({
                'override_id': override.override_id,
                'type': override.override_type.value,
                'scope': override.scope.value,
                'target': override.target,
                'operator_id': override.operator_id,
                'reason': override.reason,
                'created_ms': override.created_ms,
                'expires_ms': override.expires_ms,
                'usage_count': override.usage_count,
                'parameters': override.parameters
            })
        
        return sorted(summary, key=lambda x: x['created_ms'], reverse=True)
    
    def get_ai_learning_summary(self) -> Dict[str, Any]:
        """Get summary of AI learning and adaptations"""
        try:
            # Get learning metrics
            learning_metrics = human_alignment_ai.get_learning_metrics()
            
            # Get adaptation summary
            adaptation_summary = adaptive_alerting.get_adaptation_summary()
            
            # Get low relevance rules
            low_relevance_rules = interaction_tracker.get_low_relevance_rules()
            
            return {
                'learning_metrics': learning_metrics.__dict__,
                'recent_adaptations': adaptation_summary[:10],
                'low_relevance_rules': low_relevance_rules[:5],
                'active_overrides_count': len(self.active_overrides),
                'transparency_log_size': len(self.transparency_log)
            }
            
        except Exception as e:
            logger.error(f"Failed to get AI learning summary: {e}")
            return {}
    
    def emergency_bypass(self, operator_id: str, reason: str, duration_hours: int = 2) -> str:
        """Create emergency bypass override"""
        return self.create_override(
            OverrideType.EMERGENCY_MODE,
            OverrideScope.GLOBAL,
            OverrideDuration.TIME_LIMITED,
            "global",
            {},
            operator_id,
            reason,
            duration_hours
        )
    
    def cleanup_expired_overrides(self):
        """Clean up expired overrides"""
        now_ms = int(time.time() * 1000)
        expired_overrides = []
        
        for override_id, override in self.active_overrides.items():
            if override.expires_ms and override.expires_ms <= now_ms:
                expired_overrides.append(override_id)
        
        for override_id in expired_overrides:
            override = self.active_overrides[override_id]
            override.active = False
            
            # Update database
            con = connect()
            try:
                con.execute("UPDATE alert_overrides SET active = 0 WHERE override_id = ?", (override_id,))
                
                # Log to audit
                con.execute("""
                    INSERT INTO override_audit_log 
                    (override_id, action, operator_id, context_json, timestamp_ms)
                    VALUES (?, ?, ?, ?, ?)
                """, (override_id, 'expired', 'system', json.dumps({}), now_ms))
                
                con.commit()
            finally:
                con.close()
            
            del self.active_overrides[override_id]
        
        if expired_overrides:
            logger.info(f"Cleaned up {len(expired_overrides)} expired overrides")

# Global transparency control instance
transparency_control = AlertTransparencyControl()
