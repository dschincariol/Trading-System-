"""
Escalation Rules and Notification System

Defines escalation policies for alerts and system events:
- Tiered notification levels
- Escalation time windows
- Contact rotation and on-call schedules
- Notification channels (email, Slack, SMS, pager)
- Automatic escalation triggers
"""

import time
import json
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum
from datetime import datetime, timedelta

class EscalationLevel(Enum):
    TIER_1 = "tier_1"  # Initial alert
    TIER_2 = "tier_2"  # Escalated to senior ops
    TIER_3 = "tier_3"  # Escalated to management
    EMERGENCY = "emergency"  # Critical system failure

class NotificationChannel(Enum):
    EMAIL = "email"
    SLACK = "slack"
    SMS = "sms"
    PAGER = "pager"
    WEBHOOK = "webhook"

@dataclass
class Contact:
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    slack_user: Optional[str] = None
    is_on_call: bool = False
    escalation_level: EscalationLevel = EscalationLevel.TIER_1

@dataclass
class EscalationRule:
    name: str
    trigger_conditions: Dict[str, Any]
    escalation_levels: List[Dict[str, Any]]
    reset_conditions: Optional[Dict[str, Any]] = None
    auto_reset_minutes: Optional[int] = None

@dataclass
class EscalationEvent:
    id: str
    rule_name: str
    trigger_alert_id: str
    current_level: EscalationLevel
    started_ms: int
    last_escalated_ms: int
    resolved_ms: Optional[int] = None
    notifications_sent: List[Dict[str, Any]] = None

class EscalationManager:
    """Manages alert escalation and notification"""
    
    def __init__(self):
        self._init_tables()
        self._contacts = self._load_contacts()
        self._rules = self._load_default_rules()
        self._active_escalations = {}
    
    def _init_tables(self):
        """Initialize escalation tracking tables"""
        from engine.storage import connect
        with connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS escalation_events (
                    id TEXT PRIMARY KEY,
                    rule_name TEXT NOT NULL,
                    trigger_alert_id TEXT NOT NULL,
                    current_level TEXT NOT NULL,
                    started_ms INTEGER NOT NULL,
                    last_escalated_ms INTEGER NOT NULL,
                    resolved_ms INTEGER,
                    notifications_sent TEXT,
                    metadata_json TEXT
                )
            """)
            
            con.execute("""
                CREATE TABLE IF NOT EXISTS escalation_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    escalation_id TEXT NOT NULL,
                    level TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    contact_name TEXT NOT NULL,
                    sent_ms INTEGER NOT NULL,
                    delivered INTEGER DEFAULT 0,
                    delivery_error TEXT
                )
            """)
            
            con.execute("""
                CREATE TABLE IF NOT EXISTS on_call_schedule (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact_name TEXT NOT NULL,
                    escalation_level TEXT NOT NULL,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    is_active INTEGER DEFAULT 1
                )
            """)
    
    def _load_contacts(self) -> Dict[str, Contact]:
        """Load contact information"""
        # In production, this would load from config/database
        return {
            "ops_primary": Contact(
                name="Primary Operations",
                email="ops@tradingfirm.com",
                slack_user="@ops-primary",
                is_on_call=True,
                escalation_level=EscalationLevel.TIER_1
            ),
            "ops_senior": Contact(
                name="Senior Operations", 
                email="senior-ops@tradingfirm.com",
                slack_user="@ops-senior",
                escalation_level=EscalationLevel.TIER_2
            ),
            "trading_manager": Contact(
                name="Trading Manager",
                email="manager@tradingfirm.com", 
                phone="+1234567890",
                escalation_level=EscalationLevel.TIER_3
            ),
            "emergency_team": Contact(
                name="Emergency Response Team",
                email="emergency@tradingfirm.com",
                phone="+1234567890",
                pager="emergency-pager",
                escalation_level=EscalationLevel.EMERGENCY
            )
        }
    
    def _load_default_rules(self) -> Dict[str, EscalationRule]:
        """Load default escalation rules"""
        return {
            "critical_system_failure": EscalationRule(
                name="critical_system_failure",
                trigger_conditions={
                    "alert_severity": "critical",
                    "alert_patterns": ["system.*", "execution.*"],
                    "consecutive_alerts": 3,
                    "time_window_minutes": 5
                },
                escalation_levels=[
                    {
                        "level": EscalationLevel.TIER_1.value,
                        "delay_minutes": 0,
                        "channels": [NotificationChannel.EMAIL.value, NotificationChannel.SLACK.value],
                        "contacts": ["ops_primary"]
                    },
                    {
                        "level": EscalationLevel.TIER_2.value,
                        "delay_minutes": 5,
                        "channels": [NotificationChannel.EMAIL.value, NotificationChannel.SLACK.value],
                        "contacts": ["ops_senior"]
                    },
                    {
                        "level": EscalationLevel.TIER_3.value,
                        "delay_minutes": 15,
                        "channels": [NotificationChannel.EMAIL.value, NotificationChannel.SMS.value],
                        "contacts": ["trading_manager"]
                    },
                    {
                        "level": EscalationLevel.EMERGENCY.value,
                        "delay_minutes": 30,
                        "channels": [NotificationChannel.PAGER.value, NotificationChannel.SMS.value],
                        "contacts": ["emergency_team"]
                    }
                ],
                auto_reset_minutes=120
            ),
            
            "trading_disabled": EscalationRule(
                name="trading_disabled",
                trigger_conditions={
                    "kill_switch_active": True,
                    "duration_minutes": 10
                },
                escalation_levels=[
                    {
                        "level": EscalationLevel.TIER_1.value,
                        "delay_minutes": 0,
                        "channels": [NotificationChannel.EMAIL.value, NotificationChannel.SLACK.value],
                        "contacts": ["ops_primary"]
                    },
                    {
                        "level": EscalationLevel.TIER_2.value,
                        "delay_minutes": 10,
                        "channels": [NotificationChannel.EMAIL.value, NotificationChannel.SMS.value],
                        "contacts": ["ops_senior"]
                    }
                ],
                auto_reset_minutes=60
            ),
            
            "model_performance_degradation": EscalationRule(
                name="model_performance_degradation",
                trigger_conditions={
                    "alert_patterns": ["model.*"],
                    "severity": "warning",
                    "duration_minutes": 30
                },
                escalation_levels=[
                    {
                        "level": EscalationLevel.TIER_1.value,
                        "delay_minutes": 0,
                        "channels": [NotificationChannel.EMAIL.value],
                        "contacts": ["ops_primary"]
                    },
                    {
                        "level": EscalationLevel.TIER_2.value,
                        "delay_minutes": 30,
                        "channels": [NotificationChannel.EMAIL.value, NotificationChannel.SLACK.value],
                        "contacts": ["ops_senior"]
                    }
                ],
                auto_reset_minutes=180
            )
        }
    
    def check_escalation_triggers(self, alert_id: str) -> List[str]:
        """Check if alert triggers any escalation rules"""
        triggered_events = []
        
        # Get alert details (simplified - would integrate with alert manager)
        alert_details = self._get_alert_details(alert_id)
        if not alert_details:
            return triggered_events
        
        for rule_name, rule in self._rules.items():
            if self._should_escalate_rule(rule, alert_details):
                event_id = self._create_escalation_event(rule_name, alert_id)
                if event_id:
                    triggered_events.append(event_id)
        
        return triggered_events
    
    def _get_alert_details(self, alert_id: str) -> Optional[Dict[str, Any]]:
        """Get alert details for escalation evaluation"""
        # This would integrate with alert_manager to get full alert details
        # For now, return mock data
        return {
            "id": alert_id,
            "severity": "critical",
            "metric_name": "system.cpu_usage",
            "created_ms": int(time.time() * 1000)
        }
    
    def _should_escalate_rule(self, rule: EscalationRule, alert: Dict[str, Any]) -> bool:
        """Check if alert should trigger escalation rule"""
        conditions = rule.trigger_conditions
        
        # Check severity
        if "alert_severity" in conditions:
            if alert.get("severity") != conditions["alert_severity"]:
                return False
        
        # Check alert patterns
        if "alert_patterns" in conditions:
            metric_name = alert.get("metric_name", "")
            if not any(pattern.replace("*", "") in metric_name for pattern in conditions["alert_patterns"]):
                return False
        
        # Check consecutive alerts (simplified)
        if "consecutive_alerts" in conditions:
            # This would check for multiple similar alerts in time window
            pass
        
        return True
    
    def _create_escalation_event(self, rule_name: str, alert_id: str) -> Optional[str]:
        """Create new escalation event"""
        event_id = f"esc_{int(time.time())}"
        
        try:
            from engine.storage import connect
            with connect() as con:
                con.execute("""
                    INSERT INTO escalation_events 
                    (id, rule_name, trigger_alert_id, current_level, started_ms, last_escalated_ms)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    event_id, rule_name, alert_id, EscalationLevel.TIER_1.value,
                    int(time.time() * 1000), int(time.time() * 1000)
                ))
            
            # Start escalation process
            self._process_escalation(event_id, rule_name)
            return event_id
            
        except Exception as e:
            print(f"Failed to create escalation event: {e}")
            return None
    
    def _process_escalation(self, event_id: str, rule_name: str):
        """Process escalation through levels"""
        if rule_name not in self._rules:
            return
        
        rule = self._rules[rule_name]
        
        for level_config in rule.escalation_levels:
            level = EscalationLevel(level_config["level"])
            delay_ms = level_config["delay_minutes"] * 60 * 1000
            
            # Schedule escalation (simplified - would use proper scheduler)
            self._schedule_escalation(event_id, level, level_config, delay_ms)
    
    def _schedule_escalation(self, event_id: str, level: EscalationLevel, 
                           level_config: Dict[str, Any], delay_ms: int):
        """Schedule escalation to specific level"""
        # In production, this would use a proper task scheduler
        # For now, execute immediately if no delay
        if delay_ms == 0:
            self._execute_escalation(event_id, level, level_config)
        else:
            # Would schedule for future execution
            pass
    
    def _execute_escalation(self, event_id: str, level: EscalationLevel, 
                           level_config: Dict[str, Any]):
        """Execute escalation to specific level"""
        try:
            # Update escalation level
            from engine.storage import connect
            with connect() as con:
                con.execute("""
                    UPDATE escalation_events 
                    SET current_level = ?, last_escalated_ms = ?
                    WHERE id = ?
                """, (level.value, int(time.time() * 1000), event_id))
            
            # Send notifications
            notifications_sent = []
            for contact_name in level_config["contacts"]:
                if contact_name in self._contacts:
                    contact = self._contacts[contact_name]
                    
                    for channel_name in level_config["channels"]:
                        channel = NotificationChannel(channel_name)
                        success = self._send_notification(event_id, contact, channel)
                        
                        notifications_sent.append({
                            "contact": contact_name,
                            "channel": channel.value,
                            "success": success,
                            "timestamp": int(time.time() * 1000)
                        })
            
            # Update notifications sent
            with connect() as con:
                con.execute("""
                    UPDATE escalation_events 
                    SET notifications_sent = ?
                    WHERE id = ?
                """, (json.dumps(notifications_sent), event_id))
            
        except Exception as e:
            print(f"Failed to execute escalation: {e}")
    
    def _send_notification(self, event_id: str, contact: Contact, 
                          channel: NotificationChannel) -> bool:
        """Send notification via specified channel"""
        try:
            if channel == NotificationChannel.EMAIL:
                return self._send_email_notification(contact, event_id)
            elif channel == NotificationChannel.SLACK:
                return self._send_slack_notification(contact, event_id)
            elif channel == NotificationChannel.SMS:
                return self._send_sms_notification(contact, event_id)
            elif channel == NotificationChannel.PAGER:
                return self._send_pager_notification(contact, event_id)
            elif channel == NotificationChannel.WEBHOOK:
                return self._send_webhook_notification(contact, event_id)
            
            return False
            
        except Exception:
            return False
    
    def _send_email_notification(self, contact: Contact, event_id: str) -> bool:
        """Send email notification"""
        if not contact.email:
            return False
        
        # Implementation would use email service
        print(f"Sending email to {contact.email} for escalation {event_id}")
        return True
    
    def _send_slack_notification(self, contact: Contact, event_id: str) -> bool:
        """Send Slack notification"""
        if not contact.slack_user:
            return False
        
        # Implementation would use Slack API
        print(f"Sending Slack message to {contact.slack_user} for escalation {event_id}")
        return True
    
    def _send_sms_notification(self, contact: Contact, event_id: str) -> bool:
        """Send SMS notification"""
        if not contact.phone:
            return False
        
        # Implementation would use SMS service
        print(f"Sending SMS to {contact.phone} for escalation {event_id}")
        return True
    
    def _send_pager_notification(self, contact: Contact, event_id: str) -> bool:
        """Send pager notification"""
        # Implementation would use paging service
        print(f"Sending pager notification for escalation {event_id}")
        return True
    
    def _send_webhook_notification(self, contact: Contact, event_id: str) -> bool:
        """Send webhook notification"""
        # Implementation would send HTTP webhook
        print(f"Sending webhook notification for escalation {event_id}")
        return True
    
    def resolve_escalation(self, event_id: str, resolved_by: str = "system") -> bool:
        """Resolve escalation event"""
        try:
            from engine.storage import connect
            with connect() as con:
                con.execute("""
                    UPDATE escalation_events 
                    SET resolved_ms = ?
                    WHERE id = ?
                """, (int(time.time() * 1000), event_id))
            
            # Remove from active escalations
            self._active_escalations.pop(event_id, None)
            return True
            
        except Exception:
            return False
    
    def get_active_escalations(self) -> List[EscalationEvent]:
        """Get all active escalation events"""
        from engine.storage import connect
        with connect() as con:
            rows = con.execute("""
                SELECT id, rule_name, trigger_alert_id, current_level,
                       started_ms, last_escalated_ms, resolved_ms, notifications_sent
                FROM escalation_events 
                WHERE resolved_ms IS NULL
                ORDER BY started_ms DESC
            """).fetchall()
        
        events = []
        for row in rows:
            notifications = json.loads(row[7] or '[]')
            events.append(EscalationEvent(
                id=row[0],
                rule_name=row[1],
                trigger_alert_id=row[2],
                current_level=EscalationLevel(row[3]),
                started_ms=row[4],
                last_escalated_ms=row[5],
                resolved_ms=row[6],
                notifications_sent=notifications
            ))
        
        return events

# Global escalation manager instance
escalation_manager = EscalationManager()
