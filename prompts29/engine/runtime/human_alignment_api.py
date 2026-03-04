"""
API Endpoints for Human-Alignment AI System

Provides REST API endpoints for interaction tracking, transparency,
and override controls integration with the UI.
"""

import json
import time
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta

from flask import Flask, request, jsonify
from engine.runtime.alert_interaction_tracker import interaction_tracker, InteractionType
from engine.runtime.adaptive_alerting import adaptive_alerting
from engine.runtime.human_alignment_ai import human_alignment_ai
from engine.runtime.alert_transparency_control import transparency_control, OverrideType, OverrideScope, OverrideDuration

logger = logging.getLogger(__name__)

def register_human_alignment_routes(app: Flask):
    """Register human-alignment AI API routes"""
    
    @app.route('/api/alerts/interaction', methods=['POST'])
    def track_alert_interaction():
        """Track operator interaction with alert"""
        try:
            data = request.get_json()
            
            alert_id = data.get('alert_id')
            interaction_type = data.get('interaction_type')
            operator_id = data.get('operator_id')
            session_id = data.get('session_id')
            context = data.get('context', {})
            
            if not alert_id or not interaction_type:
                return jsonify({'error': 'Missing alert_id or interaction_type'}), 400
            
            # Convert string to enum
            try:
                interaction_enum = InteractionType(interaction_type)
            except ValueError:
                return jsonify({'error': f'Invalid interaction_type: {interaction_type}'}), 400
            
            # Track the interaction
            success = adaptive_alerting.track_operator_interaction(
                alert_id, interaction_enum, operator_id, context
            )
            
            if success:
                return jsonify({'status': 'tracked'})
            else:
                return jsonify({'error': 'Failed to track interaction'}), 500
                
        except Exception as e:
            logger.error(f"Error tracking alert interaction: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/alerts/<int:alert_id>/transparency', methods=['GET'])
    def get_alert_transparency(alert_id: int):
        """Get transparency information for alert"""
        try:
            # Get transparency records for this alert
            transparency_report = transparency_control.get_transparency_report(
                alert_id=alert_id, hours_back=24
            )
            
            return jsonify(transparency_report)
            
        except Exception as e:
            logger.error(f"Error getting alert transparency: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/overrides/emergency', methods=['POST'])
    def create_emergency_override():
        """Create emergency bypass override"""
        try:
            data = request.get_json()
            
            operator_id = data.get('operator_id')
            reason = data.get('reason')
            duration_hours = data.get('duration_hours', 2)
            
            if not operator_id or not reason:
                return jsonify({'error': 'Missing operator_id or reason'}), 400
            
            override_id = transparency_control.emergency_bypass(
                operator_id, reason, duration_hours
            )
            
            return jsonify({'override_id': override_id})
            
        except Exception as e:
            logger.error(f"Error creating emergency override: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/overrides', methods=['POST'])
    def create_override():
        """Create a custom override"""
        try:
            data = request.get_json()
            
            override_type = data.get('override_type')
            scope = data.get('scope')
            duration = data.get('duration')
            target = data.get('target')
            parameters = data.get('parameters', {})
            operator_id = data.get('operator_id')
            reason = data.get('reason')
            duration_hours = data.get('duration_hours')
            
            if not all([override_type, scope, duration, target, operator_id, reason]):
                return jsonify({'error': 'Missing required fields'}), 400
            
            # Convert strings to enums
            try:
                override_type_enum = OverrideType(override_type)
                scope_enum = OverrideScope(scope)
                duration_enum = OverrideDuration(duration)
            except ValueError as e:
                return jsonify({'error': f'Invalid enum value: {e}'}), 400
            
            override_id = transparency_control.create_override(
                override_type_enum, scope_enum, duration_enum,
                target, parameters, operator_id, reason, duration_hours
            )
            
            return jsonify({'override_id': override_id})
            
        except Exception as e:
            logger.error(f"Error creating override: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/overrides/<override_id>', methods=['DELETE'])
    def revoke_override(override_id: str):
        """Revoke an active override"""
        try:
            data = request.get_json() or {}
            operator_id = data.get('operator_id', 'unknown')
            reason = data.get('reason', 'Manual revocation')
            
            success = transparency_control.revoke_override(override_id, operator_id, reason)
            
            if success:
                return jsonify({'status': 'revoked'})
            else:
                return jsonify({'error': 'Override not found or already revoked'}), 404
                
        except Exception as e:
            logger.error(f"Error revoking override: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/overrides/active', methods=['GET'])
    def get_active_overrides():
        """Get list of active overrides"""
        try:
            overrides = transparency_control.get_active_overrides_summary()
            return jsonify(overrides)
            
        except Exception as e:
            logger.error(f"Error getting active overrides: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/transparency/report', methods=['GET'])
    def get_transparency_report():
        """Get transparency report"""
        try:
            hours_back = request.args.get('hours_back', 24, type=int)
            alert_id = request.args.get('alert_id', type=int)
            
            report = transparency_control.get_transparency_report(
                alert_id=alert_id, hours_back=hours_back
            )
            
            return jsonify(report)
            
        except Exception as e:
            logger.error(f"Error getting transparency report: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/ai/learning/summary', methods=['GET'])
    def get_ai_learning_summary():
        """Get AI learning summary"""
        try:
            summary = transparency_control.get_ai_learning_summary()
            return jsonify(summary)
            
        except Exception as e:
            logger.error(f"Error getting AI learning summary: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/ai/learning/force', methods=['POST'])
    def force_learning_cycle():
        """Force a learning cycle"""
        try:
            result = adaptive_alerting.force_learning_cycle()
            return jsonify(result)
            
        except Exception as e:
            logger.error(f"Error forcing learning cycle: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/adaptive/statistics', methods=['GET'])
    def get_adaptive_statistics():
        """Get adaptive alerting statistics"""
        try:
            stats = adaptive_alerting.get_adaptive_statistics()
            return jsonify(stats)
            
        except Exception as e:
            logger.error(f"Error getting adaptive statistics: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/adaptive/adaptations', methods=['GET'])
    def get_adaptation_summary():
        """Get adaptation summary"""
        try:
            summary = adaptive_alerting.get_adaptation_summary()
            return jsonify(summary)
            
        except Exception as e:
            logger.error(f"Error getting adaptation summary: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/adaptive/reset', methods=['POST'])
    def reset_adaptations():
        """Reset adaptations"""
        try:
            data = request.get_json() or {}
            rule_id = data.get('rule_id')
            
            adaptive_alerting.reset_adaptations(rule_id)
            
            return jsonify({'status': 'reset'})
            
        except Exception as e:
            logger.error(f"Error resetting adaptations: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/alerts/relevance/<rule_id>/<severity>/<symbol>/<int:horizon_s>', methods=['GET'])
    def get_relevance_score(rule_id: str, severity: str, symbol: str, horizon_s: int):
        """Get relevance score for specific rule configuration"""
        try:
            score = interaction_tracker.get_relevance_score(rule_id, severity, symbol, horizon_s)
            return jsonify({'relevance_score': score})
            
        except Exception as e:
            logger.error(f"Error getting relevance score: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/alerts/interaction/patterns', methods=['GET'])
    def get_interaction_patterns():
        """Get interaction patterns"""
        try:
            rule_id = request.args.get('rule_id')
            days_back = request.args.get('days_back', 7, type=int)
            
            patterns = interaction_tracker.get_interaction_patterns(rule_id, days_back)
            return jsonify(patterns)
            
        except Exception as e:
            logger.error(f"Error getting interaction patterns: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/alerts/low-relevance', methods=['GET'])
    def get_low_relevance_rules():
        """Get rules with low relevance scores"""
        try:
            min_alerts = request.args.get('min_alerts', 10, type=int)
            relevance_threshold = request.args.get('relevance_threshold', 0.3, type=float)
            
            rules = interaction_tracker.get_low_relevance_rules(min_alerts, relevance_threshold)
            return jsonify(rules)
            
        except Exception as e:
            logger.error(f"Error getting low relevance rules: {e}")
            return jsonify({'error': str(e)}), 500

# Helper function to integrate with existing Flask app
def setup_human_alignment_api(app: Flask):
    """Setup human-alignment AI API endpoints"""
    register_human_alignment_routes(app)
    logger.info("Human-alignment AI API endpoints registered")
