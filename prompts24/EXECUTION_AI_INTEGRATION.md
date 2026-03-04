# Execution Microstructure AI System - Integration Guide

## Overview

The Execution Microstructure AI system provides intelligent execution recommendations that learn optimal trading behavior from historical data. The system operates in **advisory mode only** - no automatic execution authority.

## Architecture

### Core Components

1. **Feature Engineering** (`execution_ai_features.py`)
   - Extracts 20+ predictive features from market data
   - Market microstructure, temporal, liquidity, and alpha decay features
   - Real-time feature calculation for new orders

2. **AI Decision Model** (`execution_ai_model.py`)
   - Machine learning models for slippage prediction
   - Execution action recommendations (slice, delay, cross, avoid)
   - Continuous learning from execution outcomes

3. **Safety Advisor** (`execution_ai_advisor.py`)
   - Advisory-only interface with manual approval required
   - Recommendation lifecycle management
   - Audit logging and outcome tracking

4. **Broker Integration** (`broker_ai_integration.py`)
   - Safe integration with existing broker adapters
   - No automatic execution - all recommendations need approval
   - Backward compatible with existing order flow

5. **Dashboard Analytics** (`execution_ai_dashboard.py`)
   - Real-time performance monitoring
   - Model diagnostics and feature analysis
   - Recommendation effectiveness tracking

6. **Training Job** (`jobs/train_execution_ai.py`)
   - Automated model retraining
   - Scheduled training with quality checks
   - Performance monitoring and rollback capability

## Integration Steps

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Initialize Database Tables

The system automatically creates required tables on first run. Key tables:

- `execution_features` - Feature storage for training
- `execution_ai_advisory` - Recommendation tracking
- `execution_model_training` - Model metadata
- `execution_ai_audit_log` - Audit trail

### 3. Train Initial Model

```python
from engine.execution.jobs.train_execution_ai import force_training

# Train initial model with 30 days of data
result = force_training(lookback_days=30, min_samples=100)
print(f"Training result: {result}")
```

### 4. Integrate with Broker Adapters

Modify existing broker adapters to use AI recommendations:

```python
from engine.execution.broker_ai_integration import (
    enhance_order_with_ai,
    submit_order_with_ai_safety,
    record_fill_with_ai_tracking
)

# Before submitting order
enhanced_params = enhance_order_with_ai(
    broker_name="alpaca",
    client_order_id=order_id,
    symbol="AAPL",
    qty=100,
    aggressiveness="MEDIUM"
)

# Submit with AI safety checks
result = submit_order_with_ai_safety(
    broker_name="alpaca",
    client_order_id=order_id,
    symbol="AAPL",
    qty=100,
    order_type="LIMIT",
    aggressiveness="MEDIUM",
    limit_px=150.0
)

# Record fills with AI outcome tracking
record_fill_with_ai_tracking(
    broker_name="alpaca",
    client_order_id=order_id,
    fill_id="fill_123",
    fill_px=150.25,
    fill_qty=100,
    fill_ts_ms=int(time.time() * 1000),
    fees=0.15
)
```

### 5. Set Up Approval Workflow

Create approval process for AI recommendations:

```python
from engine.execution.execution_ai_advisor import (
    get_pending_ai_recommendations,
    approve_ai_recommendation,
    reject_ai_recommendation
)

# Get pending recommendations
pending = get_pending_ai_recommendations(broker="alpaca")

# Approve recommendation
approve_ai_recommendation(
    recommendation_id="ai_adv_123",
    approved_by="trader_desk"
)

# Reject recommendation
reject_ai_recommendation(
    recommendation_id="ai_adv_124",
    rejected_by="trader_desk",
    reason="Market conditions changed"
)
```

### 6. Configure Dashboard

Add AI analytics to existing dashboard:

```python
from engine.execution.execution_ai_dashboard import (
    get_ai_dashboard_data,
    get_ai_action_performance,
    get_ai_model_diagnostics
)

# Get dashboard data
dashboard_data = get_ai_dashboard_data(days=7)

# Get action performance breakdown
action_perf = get_ai_action_performance(days=7)

# Get model diagnostics
diagnostics = get_ai_model_diagnostics()
```

## Safety Features

### Advisory Mode Only
- **No automatic execution** - all recommendations require manual approval
- **Explicit approval workflow** - recommendations expire after TTL
- **Audit logging** - all actions tracked for compliance

### Model Safety
- **Minimum sample requirements** - model won't train with insufficient data
- **Performance monitoring** - automatic rollback if performance degrades
- **Feature validation** - robust handling of missing or invalid data

### Integration Safety
- **Fail-soft design** - AI failures don't block normal execution
- **Backward compatibility** - existing order flow continues unchanged
- **Gradual rollout** - can enable/disable per broker or symbol

## Configuration

### Environment Variables

```bash
# AI Model Configuration
EPE_AI_MODEL_TYPE=random_forest  # random_forest, gradient_boost, linear
EPE_AI_TRAINING_INTERVAL_HOURS=24
EPE_AI_MIN_SAMPLES=100
EPE_AI_ADVISORY_TTL_MS=60000  # 1 minute

# Safety Configuration
EPE_AI_REQUIRE_APPROVAL=true
EPE_AI_AUTO_APPROVE_THRESHOLD=0.8  # Disabled by default
EPE_AI_MAX_RISK_SLIPPAGE_BPS=10.0
```

### Training Schedule

```python
from engine.execution.jobs.train_execution_ai import update_training_schedule

# Configure training schedule
update_training_schedule(
    training_interval_hours=12,  # Train every 12 hours
    min_samples_required=200,    # Require 200 labeled samples
    auto_train_enabled=True      # Enable automatic training
)
```

## Monitoring

### Key Metrics

1. **Model Performance**
   - Training/validation MAE
   - Slippage prediction accuracy
   - Feature importance drift

2. **Advisory Effectiveness**
   - Approval rate
   - Recommendation accuracy
   - Cost improvement vs baseline

3. **Execution Quality**
   - Realized vs predicted slippage
   - Fill probability accuracy
   - Alpha preservation

### Alerts

Set up monitoring for:

- Model performance degradation (>20% increase in MAE)
- Low approval rates (<30% for high-confidence recommendations)
- Feature data quality issues
- Training job failures

## Troubleshooting

### Common Issues

1. **Model Not Trained**
   ```python
   from engine.execution.execution_ai_model import is_model_trained
   if not is_model_trained():
       print("Model needs training - run force_training()")
   ```

2. **Insufficient Training Data**
   ```python
   from engine.execution.execution_ai_features import get_training_data
   X, y = get_training_data(lookback_days=30, min_samples=0)
   print(f"Available samples: {len(X)}")
   ```

3. **AI Recommendations Not Generated**
   - Check if model is trained
   - Verify feature extraction is working
   - Check advisory configuration

### Debug Mode

Enable detailed logging:

```python
import logging
logging.getLogger('engine.execution').setLevel(logging.DEBUG)
```

## Performance Considerations

### Feature Extraction
- Cached market data to reduce database queries
- Batch feature calculation for multiple orders
- Optimized volatility calculations

### Model Training
- Incremental training for large datasets
- Feature selection to reduce dimensionality
- Parallel training for multiple models

### Real-time Recommendations
- Pre-computed features for common symbols
- Model caching in memory
- Async recommendation generation

## Compliance

### Audit Trail
All AI recommendations and actions are logged:
- Recommendation generation and approval
- Feature data used for decisions
- Execution outcomes and learning
- Model training and version changes

### Data Privacy
- No personal or account data in features
- Aggregated market data only
- Configurable data retention policies

### Model Governance
- Model version control and rollback
- Performance monitoring and alerts
- Documentation of model changes

## Future Enhancements

### Advanced Features
- Multi-asset execution optimization
- Real-time market regime detection
- Dynamic risk-adjusted execution
- Cross-venue liquidity aggregation

### Model Improvements
- Deep learning for complex patterns
- Reinforcement learning for adaptive strategies
- Ensemble models for robustness
- Online learning for real-time updates

### Integration Expansion
- Additional broker adapters
- FIX protocol integration
- Portfolio-level optimization
- Multi-strategy coordination
