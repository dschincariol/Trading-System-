"""
Human Alignment AI - Learning Loop for Alert Relevance

Implements machine learning to understand operator behavior patterns
and adapt alert thresholds for optimal signal-to-noise ratio.
"""

import numpy as np
import pandas as pd
import json
import time
import logging
from typing import Dict, List, Tuple, Optional, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timedelta
from collections import defaultdict, deque
import sqlite3
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, confusion_matrix

from engine.storage import connect
from engine.runtime.alert_interaction_tracker import interaction_tracker, InteractionType, AlertOutcome

logger = logging.getLogger(__name__)

class LearningStrategy(Enum):
    """Learning strategies for alert adaptation"""
    RELEVANCE_BASED = "relevance_based"      # Adapt based on relevance scores
    OUTCOME_BASED = "outcome_based"          # Adapt based on alert outcomes
    HYBRID = "hybrid"                        # Combine relevance and outcomes
    CONSERVATIVE = "conservative"            # Minimal adaptation, safety-first

class AdaptationAction(Enum):
    """Types of adaptations that can be made"""
    INCREASE_THRESHOLD = "increase_threshold"
    DECREASE_THRESHOLD = "decrease_threshold"
    INCREASE_COOLDOWN = "increase_cooldown"
    DECREASE_COOLDOWN = "decrease_cooldown"
    CHANGE_SEVERITY = "change_severity"
    NO_CHANGE = "no_change"

@dataclass
class LearningConfig:
    """Configuration for learning system"""
    strategy: LearningStrategy = LearningStrategy.HYBRID
    min_interactions_for_learning: int = 10
    learning_window_days: int = 30
    adaptation_sensitivity: float = 0.1  # How aggressively to adapt
    safety_margin: float = 0.2  # Safety buffer for critical alerts
    max_threshold_adjustment: float = 0.5  # Max 50% adjustment
    confidence_threshold: float = 0.7  # Confidence needed for adaptation
    
@dataclass
class AdaptationRecommendation:
    """Recommended adaptation for alert rule"""
    rule_id: str
    severity: str
    symbol: str
    horizon_s: int
    action: AdaptationAction
    current_value: float
    recommended_value: float
    confidence: float
    reasoning: str
    evidence: Dict[str, Any] = field(default_factory=dict)

@dataclass
class LearningMetrics:
    """Metrics for learning system performance"""
    total_adaptations: int = 0
    successful_adaptations: int = 0
    false_positive_reduction: float = 0.0
    signal_preservation: float = 0.0
    operator_satisfaction: float = 0.0
    last_learning_cycle: datetime = field(default_factory=datetime.now)

class HumanAlignmentAI:
    """Main learning system for human-alignment of alerts"""
    
    def __init__(self, config: Optional[LearningConfig] = None):
        self.config = config or LearningConfig()
        self.metrics = LearningMetrics()
        
        # Machine learning components
        self.relevance_model = RandomForestClassifier(n_estimators=100, random_state=42)
        self.scaler = StandardScaler()
        self.model_trained = False
        
        # Learning history
        self.adaptation_history: deque = deque(maxlen=1000)
        self.learning_cycles: List[Dict[str, Any]] = []
        
        # Cache for relevance scores
        self._relevance_cache: Dict[str, float] = {}
        self._cache_timestamp_ms = 0
        self._cache_ttl_ms = 300000  # 5 minutes
        
        logger.info("HumanAlignmentAI initialized")
    
    def learn_from_interactions(self) -> List[AdaptationRecommendation]:
        """Main learning loop - analyze interactions and generate recommendations"""
        try:
            logger.info("Starting learning cycle from operator interactions")
            
            # Gather learning data
            learning_data = self._gather_learning_data()
            if not learning_data:
                logger.info("Insufficient data for learning")
                return []
            
            # Analyze patterns and generate recommendations
            recommendations = []
            
            if self.config.strategy in [LearningStrategy.RELEVANCE_BASED, LearningStrategy.HYBRID]:
                recs = self._analyze_relevance_patterns(learning_data)
                recommendations.extend(recs)
            
            if self.config.strategy in [LearningStrategy.OUTCOME_BASED, LearningStrategy.HYBRID]:
                recs = self._analyze_outcome_patterns(learning_data)
                recommendations.extend(recs)
            
            # Filter and rank recommendations
            filtered_recommendations = self._filter_recommendations(recommendations)
            
            # Update metrics
            self.metrics.last_learning_cycle = datetime.now()
            self.learning_cycles.append({
                'timestamp': datetime.now().isoformat(),
                'data_points': len(learning_data),
                'recommendations': len(filtered_recommendations),
                'strategy': self.config.strategy.value
            })
            
            logger.info(f"Learning cycle completed: {len(filtered_recommendations)} recommendations")
            return filtered_recommendations
            
        except Exception as e:
            logger.error(f"Learning cycle failed: {e}")
            return []
    
    def _gather_learning_data(self) -> List[Dict[str, Any]]:
        """Gather interaction data for learning"""
        cutoff_ms = int((datetime.now() - timedelta(days=self.config.learning_window_days)).timestamp() * 1000)
        
        con = connect()
        try:
            # Get comprehensive interaction data
            query = """
                SELECT 
                    a.id as alert_id,
                    a.rule_id,
                    a.severity,
                    a.symbol,
                    a.horizon_s,
                    a.expected_z,
                    a.confidence,
                    a.ts_ms as created_ms,
                    COUNT(ai.id) as interaction_count,
                    SUM(CASE WHEN ai.interaction_type = 'click' THEN 1 ELSE 0 END) as clicks,
                    SUM(CASE WHEN ai.interaction_type = 'acknowledge' THEN 1 ELSE 0 END) as acknowledges,
                    SUM(CASE WHEN ai.interaction_type = 'ignore' THEN 1 ELSE 0 END) as ignores,
                    SUM(CASE WHEN ai.interaction_type = 'action' THEN 1 ELSE 0 END) as actions,
                    SUM(CASE WHEN ai.interaction_type = 'false_positive' THEN 1 ELSE 0 END) as false_positives,
                    AVG(ai.time_to_interaction_ms) as avg_time_to_interaction,
                    al.outcome,
                    al.resolved_ms
                FROM alerts a
                LEFT JOIN alert_interactions ai ON a.id = ai.alert_id
                LEFT JOIN alert_lifecycles al ON a.id = al.alert_id
                WHERE a.ts_ms >= ?
                GROUP BY a.id
                HAVING interaction_count > 0 OR al.outcome IS NOT NULL
            """
            
            rows = con.execute(query, (cutoff_ms,)).fetchall()
            
            learning_data = []
            for row in rows:
                learning_data.append({
                    'alert_id': row[0],
                    'rule_id': row[1],
                    'severity': row[2],
                    'symbol': row[3],
                    'horizon_s': row[4],
                    'expected_z': row[5],
                    'confidence': row[6],
                    'created_ms': row[7],
                    'interaction_count': row[8],
                    'clicks': row[9],
                    'acknowledges': row[10],
                    'ignores': row[11],
                    'actions': row[12],
                    'false_positives': row[13],
                    'avg_time_to_interaction': row[14],
                    'outcome': row[15],
                    'resolved_ms': row[16]
                })
            
            return learning_data
            
        finally:
            con.close()
    
    def _analyze_relevance_patterns(self, learning_data: List[Dict[str, Any]]) -> List[AdaptationRecommendation]:
        """Analyze patterns based on relevance scores"""
        recommendations = []
        
        # Group by rule configuration
        rule_groups = defaultdict(list)
        for data in learning_data:
            key = f"{data['rule_id']}_{data['severity']}_{data['symbol']}_{data['horizon_s']}"
            rule_groups[key].append(data)
        
        for rule_key, rule_data in rule_groups.items():
            if len(rule_data) < self.config.min_interactions_for_learning:
                continue
            
            # Calculate metrics for this rule
            total_alerts = len(rule_data)
            click_rate = sum(d['clicks'] for d in rule_data) / total_alerts
            ignore_rate = sum(d['ignores'] for d in rule_data) / total_alerts
            false_positive_rate = sum(d['false_positives'] for d in rule_data) / total_alerts
            action_rate = sum(d['actions'] for d in rule_data) / total_alerts
            
            # Calculate relevance score
            relevance_score = self._calculate_rule_relevance(
                click_rate, ignore_rate, false_positive_rate, action_rate
            )
            
            # Generate recommendations based on relevance
            sample = rule_data[0]
            
            if relevance_score < 0.3 and false_positive_rate > 0.4:
                # High false positive rate - increase threshold
                recommendations.append(self._create_threshold_recommendation(
                    sample, AdaptationAction.INCREASE_THRESHOLD, 
                    "High false positive rate and low relevance",
                    {'relevance_score': relevance_score, 'false_positive_rate': false_positive_rate}
                ))
            
            elif relevance_score < 0.3 and ignore_rate > 0.6:
                # High ignore rate - increase threshold or cooldown
                if sample['severity'] != 'CRIT':
                    recommendations.append(self._create_cooldown_recommendation(
                        sample, AdaptationAction.INCREASE_COOLDOWN,
                        "High ignore rate, operators not finding these alerts useful",
                        {'relevance_score': relevance_score, 'ignore_rate': ignore_rate}
                    ))
            
            elif relevance_score > 0.8 and action_rate > 0.5:
                # High relevance and action rate - could be more aggressive
                if sample['severity'] == 'INFO':
                    recommendations.append(self._create_severity_recommendation(
                        sample, 'WARN',
                        "High relevance and action rate, consider elevation",
                        {'relevance_score': relevance_score, 'action_rate': action_rate}
                    ))
        
        return recommendations
    
    def _analyze_outcome_patterns(self, learning_data: List[Dict[str, Any]]) -> List[AdaptationRecommendation]:
        """Analyze patterns based on alert outcomes"""
        recommendations = []
        
        # Group by rule configuration
        rule_groups = defaultdict(list)
        for data in learning_data:
            key = f"{data['rule_id']}_{data['severity']}_{data['symbol']}_{data['horizon_s']}"
            rule_groups[key].append(data)
        
        for rule_key, rule_data in rule_groups.items():
            if len(rule_data) < self.config.min_interactions_for_learning:
                continue
            
            # Calculate outcome metrics
            resolved_alerts = [d for d in rule_data if d['outcome'] is not None]
            if not resolved_alerts:
                continue
            
            total_resolved = len(resolved_alerts)
            true_positive_rate = sum(1 for d in resolved_alerts if d['outcome'] == 'true_positive') / total_resolved
            false_positive_rate = sum(1 for d in resolved_alerts if d['outcome'] == 'false_positive') / total_resolved
            actionable_rate = sum(1 for d in resolved_alerts if d['outcome'] == 'actionable') / total_resolved
            
            sample = rule_data[0]
            
            # Generate recommendations based on outcomes
            if false_positive_rate > 0.5:
                recommendations.append(self._create_threshold_recommendation(
                    sample, AdaptationAction.INCREASE_THRESHOLD,
                    "High false positive outcome rate",
                    {'false_positive_outcome_rate': false_positive_rate}
                ))
            
            elif true_positive_rate > 0.8 and actionable_rate > 0.6:
                # High true positive and actionable rate
                if sample['severity'] in ['INFO', 'WARN']:
                    recommendations.append(self._create_severity_recommendation(
                        sample, 'HIGH' if sample['severity'] == 'WARN' else 'WARN',
                        "High true positive and actionable rate",
                        {'true_positive_rate': true_positive_rate, 'actionable_rate': actionable_rate}
                    ))
        
        return recommendations
    
    def _calculate_rule_relevance(self, click_rate: float, ignore_rate: float, 
                                false_positive_rate: float, action_rate: float) -> float:
        """Calculate relevance score for a rule"""
        # Positive signals
        positive_score = (click_rate * 0.3 + action_rate * 0.5)
        
        # Negative signals
        negative_score = (ignore_rate * 0.3 + false_positive_rate * 0.7)
        
        # Normalize to [0, 1]
        relevance = positive_score - negative_score
        return max(0.0, min(1.0, relevance))
    
    def _create_threshold_recommendation(self, sample: Dict[str, Any], action: AdaptationAction,
                                       reasoning: str, evidence: Dict[str, Any]) -> AdaptationRecommendation:
        """Create threshold adaptation recommendation"""
        # Get current threshold from rule
        current_threshold = self._get_rule_threshold(sample['rule_id'])
        
        # Calculate adjustment
        adjustment_factor = 1.0 + (self.config.adaptation_sensitivity * 0.2)
        if action == AdaptationAction.INCREASE_THRESHOLD:
            recommended_value = current_threshold * adjustment_factor
        else:
            recommended_value = current_threshold / adjustment_factor
        
        # Cap the adjustment
        max_change = current_threshold * self.config.max_threshold_adjustment
        recommended_value = min(current_threshold + max_change, 
                               max(current_threshold - max_change, recommended_value))
        
        confidence = min(0.9, len([v for v in evidence.values() if v > 0.5]) * 0.3)
        
        return AdaptationRecommendation(
            rule_id=sample['rule_id'],
            severity=sample['severity'],
            symbol=sample['symbol'],
            horizon_s=sample['horizon_s'],
            action=action,
            current_value=current_threshold,
            recommended_value=recommended_value,
            confidence=confidence,
            reasoning=reasoning,
            evidence=evidence
        )
    
    def _create_cooldown_recommendation(self, sample: Dict[str, Any], action: AdaptationAction,
                                       reasoning: str, evidence: Dict[str, Any]) -> AdaptationRecommendation:
        """Create cooldown adaptation recommendation"""
        current_cooldown = self._get_rule_cooldown(sample['severity'])
        
        adjustment_factor = 1.0 + (self.config.adaptation_sensitivity * 0.3)
        if action == AdaptationAction.INCREASE_COOLDOWN:
            recommended_value = current_cooldown * adjustment_factor
        else:
            recommended_value = current_cooldown / adjustment_factor
        
        confidence = min(0.8, len([v for v in evidence.values() if v > 0.5]) * 0.25)
        
        return AdaptationRecommendation(
            rule_id=sample['rule_id'],
            severity=sample['severity'],
            symbol=sample['symbol'],
            horizon_s=sample['horizon_s'],
            action=action,
            current_value=current_cooldown,
            recommended_value=recommended_value,
            confidence=confidence,
            reasoning=reasoning,
            evidence=evidence
        )
    
    def _create_severity_recommendation(self, sample: Dict[str, Any], new_severity: str,
                                      reasoning: str, evidence: Dict[str, Any]) -> AdaptationRecommendation:
        """Create severity adaptation recommendation"""
        confidence = min(0.7, len([v for v in evidence.values() if v > 0.6]) * 0.2)
        
        return AdaptationRecommendation(
            rule_id=sample['rule_id'],
            severity=sample['severity'],
            symbol=sample['symbol'],
            horizon_s=sample['horizon_s'],
            action=AdaptationAction.CHANGE_SEVERITY,
            current_value=sample['severity'],
            recommended_value=new_severity,
            confidence=confidence,
            reasoning=reasoning,
            evidence=evidence
        )
    
    def _get_rule_threshold(self, rule_id: str) -> float:
        """Get current threshold for rule"""
        # This would typically come from rule configuration
        # For now, return default values based on rule patterns
        if 'z1' in rule_id:
            return 1.0
        elif 'z15' in rule_id:
            return 1.5
        elif 'z2' in rule_id:
            return 2.0
        else:
            return 1.0
    
    def _get_rule_cooldown(self, severity: str) -> int:
        """Get current cooldown for severity"""
        from engine.runtime.alerts import COOLDOWN_WARN_S, COOLDOWN_HIGH_S, COOLDOWN_CRIT_S
        
        if severity == 'WARN':
            return COOLDOWN_WARN_S
        elif severity == 'HIGH':
            return COOLDOWN_HIGH_S
        elif severity == 'CRIT':
            return COOLDOWN_CRIT_S
        else:
            return 300  # Default 5 minutes
    
    def _filter_recommendations(self, recommendations: List[AdaptationRecommendation]) -> List[AdaptationRecommendation]:
        """Filter and rank recommendations"""
        filtered = []
        
        for rec in recommendations:
            # Skip if confidence too low
            if rec.confidence < self.config.confidence_threshold:
                continue
            
            # Skip critical alert suppressions
            if rec.severity == 'CRIT' and rec.action in [AdaptationAction.INCREASE_THRESHOLD, AdaptationAction.INCREASE_COOLDOWN]:
                continue
            
            # Apply safety margin for critical infrastructure
            if rec.severity in ['HIGH', 'CRIT'] and rec.action == AdaptationAction.INCREASE_THRESHOLD:
                rec.recommended_value = min(rec.recommended_value, 
                                           rec.current_value * (1 + self.config.safety_margin))
            
            filtered.append(rec)
        
        # Sort by confidence and relevance impact
        filtered.sort(key=lambda x: (x.confidence, x.evidence.get('relevance_score', 0)), reverse=True)
        
        return filtered[:10]  # Limit to top 10 recommendations
    
    def apply_adaptation(self, recommendation: AdaptationRecommendation, operator_id: str) -> bool:
        """Apply an adaptation recommendation"""
        try:
            # Log the adaptation
            self._log_adaptation(recommendation, operator_id)
            
            # Apply the change based on action type
            success = False
            
            if recommendation.action == AdaptationAction.INCREASE_THRESHOLD:
                success = self._apply_threshold_change(recommendation, operator_id)
            elif recommendation.action == AdaptationAction.DECREASE_THRESHOLD:
                success = self._apply_threshold_change(recommendation, operator_id)
            elif recommendation.action == AdaptationAction.INCREASE_COOLDOWN:
                success = self._apply_cooldown_change(recommendation, operator_id)
            elif recommendation.action == AdaptationAction.DECREASE_COOLDOWN:
                success = self._apply_cooldown_change(recommendation, operator_id)
            elif recommendation.action == AdaptationAction.CHANGE_SEVERITY:
                success = self._apply_severity_change(recommendation, operator_id)
            
            if success:
                self.metrics.total_adaptations += 1
                self.adaptation_history.append({
                    'timestamp': datetime.now().isoformat(),
                    'recommendation': recommendation,
                    'operator_id': operator_id,
                    'success': True
                })
                
                logger.info(f"Applied adaptation: {recommendation.action.value} for rule {recommendation.rule_id}")
            
            return success
            
        except Exception as e:
            logger.error(f"Failed to apply adaptation: {e}")
            return False
    
    def _log_adaptation(self, recommendation: AdaptationRecommendation, operator_id: str):
        """Log adaptation to database"""
        con = connect()
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS alert_adaptations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    horizon_s INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    current_value REAL NOT NULL,
                    recommended_value REAL NOT NULL,
                    confidence REAL NOT NULL,
                    reasoning TEXT NOT NULL,
                    evidence_json TEXT,
                    operator_id TEXT NOT NULL,
                    applied_ms INTEGER NOT NULL,
                    status TEXT DEFAULT 'pending'
                )
            """)
            
            con.execute("""
                INSERT INTO alert_adaptations 
                (rule_id, severity, symbol, horizon_s, action, current_value, recommended_value,
                 confidence, reasoning, evidence_json, operator_id, applied_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                recommendation.rule_id, recommendation.severity, recommendation.symbol,
                recommendation.horizon_s, recommendation.action.value, recommendation.current_value,
                recommendation.recommended_value, recommendation.confidence, recommendation.reasoning,
                json.dumps(recommendation.evidence), operator_id, int(time.time() * 1000)
            ))
            
            con.commit()
        finally:
            con.close()
    
    def _apply_threshold_change(self, recommendation: AdaptationRecommendation, operator_id: str) -> bool:
        """Apply threshold change (placeholder - would integrate with rule system)"""
        # This would integrate with the actual rule configuration system
        # For now, just log the intended change
        logger.info(f"Threshold change for {recommendation.rule_id}: {recommendation.current_value} -> {recommendation.recommended_value}")
        return True
    
    def _apply_cooldown_change(self, recommendation: AdaptationRecommendation, operator_id: str) -> bool:
        """Apply cooldown change (placeholder - would integrate with alert system)"""
        # This would integrate with the actual alert cooldown system
        logger.info(f"Cooldown change for {recommendation.severity}: {recommendation.current_value}s -> {recommendation.recommended_value}s")
        return True
    
    def _apply_severity_change(self, recommendation: AdaptationRecommendation, operator_id: str) -> bool:
        """Apply severity change (placeholder - would integrate with rule system)"""
        # This would integrate with the actual rule severity system
        logger.info(f"Severity change for {recommendation.rule_id}: {recommendation.current_value} -> {recommendation.recommended_value}")
        return True
    
    def get_learning_metrics(self) -> LearningMetrics:
        """Get current learning metrics"""
        return self.metrics
    
    def get_adaptation_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent adaptation history"""
        return list(self.adaptation_history)[-limit:]

# Global AI instance
human_alignment_ai = HumanAlignmentAI()
