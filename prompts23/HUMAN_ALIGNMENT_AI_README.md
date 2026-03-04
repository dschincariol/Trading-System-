# Human-Alignment AI for Trading Operations

## Overview

This system implements a human-alignment AI that learns how operators interact with alerts and explanations, adapting alert thresholds and verbosity to improve signal-to-noise ratio while maintaining safety.

## Architecture

### Core Components

1. **Alert Interaction Tracker** (`alert_interaction_tracker.py`)
   - Tracks user interactions (clicks, acknowledges, ignores, false positives)
   - Maintains alert lifecycle data
   - Calculates relevance scores for rule configurations

2. **Human Alignment AI** (`human_alignment_ai.py`)
   - Machine learning system that analyzes interaction patterns
   - Generates adaptation recommendations
   - Implements multiple learning strategies (relevance-based, outcome-based, hybrid)

3. **Adaptive Alerting** (`adaptive_alerting.py`)
   - Integrates AI recommendations with alert generation
   - Applies learned adaptations while maintaining safety constraints
   - Ensures critical alerts are never suppressed

4. **Transparency & Override Controls** (`alert_transparency_control.py`)
   - Provides transparency into AI decisions
   - Allows human operators to override AI behavior
   - Maintains audit logs and emergency bypass capabilities

5. **Enhanced UI** (`alerts_enhanced.js`)
   - Tracks user interactions automatically
   - Displays relevance scores and AI decisions
   - Provides override controls for operators

6. **API Layer** (`human_alignment_api.py`)
   - REST API endpoints for UI integration
   - Handles interaction tracking, transparency, and overrides

## Key Features

### ✅ Interaction Signals
- **Click signals**: User clicks alert for details
- **Acknowledge signals**: User marks alert as seen
- **Ignore signals**: User dismisses without action
- **Action signals**: User takes action based on alert
- **False positive signals**: User marks as false positive

### ✅ Learning Loop
- Analyzes interaction patterns every 24 hours or when sufficient data collected
- Calculates relevance scores based on click, ignore, and action rates
- Generates adaptation recommendations with confidence scores
- Supports multiple learning strategies

### ✅ Adaptive Alerting
- Dynamically adjusts alert thresholds based on learned relevance
- Applies confidence adjustments and severity upgrades
- Maintains safety constraints for critical alerts
- Filters low-relevance alerts to reduce noise

### ✅ Critical Alert Safety
- **Never suppresses critical alerts** - CRITICAL alerts always pass through
- Relaxed cooldown for HIGH severity alerts
- Quality gate warnings instead of blocking for HIGH/CRITICAL
- Minimum safe thresholds enforced by severity level

### ✅ Transparency & Controls
- Full transparency into AI decision-making
- Operator override capabilities (emergency bypass, rule blocking, threshold adjustment)
- Comprehensive audit logging
- Real-time relevance score display

## Safety Constraints

### Critical Alert Protection
```python
# Critical alerts always pass through
if severity == 'CRIT':
    return True  # Never suppress critical alerts

# High severity gets relaxed treatment
if severity == 'HIGH':
    # Reduced cooldown, quality warnings instead of blocking
```

### Threshold Safety
```python
# Minimum safe thresholds by severity
safe_thresholds = {
    'INFO': 0.5,    # Can be filtered aggressively
    'WARN': 0.8,    # Moderate safety
    'HIGH': 1.2,    # High safety, but can be adjusted
    'CRIT': 1.5     # Never reduced below this
}
```

### Override Controls
- Emergency bypass for critical situations
- Time-limited overrides with automatic expiration
- Comprehensive audit logging
- Operator authentication required

## Usage Examples

### Emitting Adaptive Alerts
```python
from engine.runtime.adaptive_alerting import adaptive_alerting

# Replace existing emit_alert calls with adaptive version
alert_id = adaptive_alerting.emit_adaptive_alert(
    event_title="Model drift detected",
    symbol="AAPL",
    horizon_s=3600,
    expected_z=1.8,
    confidence=0.75,
    explain={"drift_score": 0.85}
)
```

### Tracking Interactions
```python
from engine.runtime.alert_interaction_tracker import interaction_tracker, InteractionType

# Track user interaction
success = interaction_tracker.track_interaction(
    alert_id=12345,
    interaction_type=InteractionType.ACKNOWLEDGE,
    operator_id="operator_001",
    context={"severity": "WARN", "rule_id": "warn_z1_conf55"}
)
```

### Creating Overrides
```python
from engine.runtime.alert_transparency_control import transparency_control, OverrideType

# Create emergency bypass
override_id = transparency_control.emergency_bypass(
    operator_id="operator_001",
    reason="Market volatility - need all alerts",
    duration_hours=2
)
```

### UI Integration
```javascript
import { initializeInteractionTracking, trackAlertInteraction } from './alerts_enhanced.js';

// Initialize tracking
initializeInteractionTracking('operator_001');

// Track interactions automatically
await trackAlertInteraction(alertId, 'click', {
    severity: 'WARN',
    symbol: 'AAPL'
});
```

## Configuration

### Learning Configuration
```python
from engine.runtime.human_alignment_ai import LearningConfig, LearningStrategy

config = LearningConfig(
    strategy=LearningStrategy.HYBRID,
    min_interactions_for_learning=10,
    learning_window_days=30,
    adaptation_sensitivity=0.1,
    safety_margin=0.2,
    confidence_threshold=0.7
)
```

### Adaptive Alerting Configuration
```python
from engine.runtime.adaptive_alerting import AdaptiveAlertConfig

config = AdaptiveAlertConfig(
    enable_adaptive_thresholds=True,
    enable_relevance_filtering=True,
    min_relevance_threshold=0.3,
    critical_safety_margin=0.1,
    learning_integration=True
)
```

## Database Schema

The system creates several new tables:

- `alert_interactions` - Tracks user interactions
- `alert_lifecycles` - Complete alert lifecycle data
- `alert_interaction_stats` - Aggregated statistics for learning
- `alert_adaptations` - Applied adaptations with audit trail
- `adaptive_alert_tracking` - Tracks adaptive alert generation
- `alert_overrides` - Operator override configurations
- `alert_transparency_log` - AI decision transparency
- `override_audit_log` - Override audit trail

## API Endpoints

### Interaction Tracking
- `POST /api/alerts/interaction` - Track user interaction
- `GET /api/alerts/<id>/transparency` - Get alert transparency

### Overrides
- `POST /api/overrides/emergency` - Create emergency bypass
- `POST /api/overrides` - Create custom override
- `DELETE /api/overrides/<id>` - Revoke override
- `GET /api/overrides/active` - List active overrides

### AI & Learning
- `GET /api/ai/learning/summary` - Get learning summary
- `POST /api/ai/learning/force` - Force learning cycle
- `GET /api/adaptive/statistics` - Get adaptive statistics
- `POST /api/adaptive/reset` - Reset adaptations

### Analytics
- `GET /api/alerts/relevance/<rule>/<severity>/<symbol>/<horizon>` - Get relevance score
- `GET /api/alerts/interaction/patterns` - Get interaction patterns
- `GET /api/alerts/low-relevance` - Get low relevance rules

## Monitoring & Metrics

The system provides comprehensive metrics:

- Total alerts evaluated vs emitted
- Adaptive adjustments applied
- Relevance filters applied
- Learning cycle performance
- Override usage statistics
- Operator satisfaction metrics

## Integration Steps

1. **Install Dependencies**: Ensure scikit-learn is available for ML components
2. **Initialize Database**: Run system to create new tables
3. **Update Alert Generation**: Replace `emit_alert` calls with `emit_adaptive_alert`
4. **Integrate UI**: Use enhanced alerts.js for interaction tracking
5. **Setup API**: Register API endpoints with Flask app
6. **Configure Learning**: Adjust learning parameters for your environment
7. **Monitor Performance**: Track metrics and adjust thresholds as needed

## Safety Considerations

- **Advisory Only**: System provides recommendations, human operators maintain control
- **No Critical Suppression**: Critical alerts are never suppressed by AI
- **Conservative Defaults**: System starts with conservative adaptation settings
- **Comprehensive Logging**: All decisions and overrides are fully auditable
- **Emergency Controls**: Multiple layers of override and emergency bypass capabilities

This system provides a robust, safety-first approach to improving alert signal-to-noise ratio through human-alignment AI while maintaining operator control and system safety.
