# Production Model Governance System

A comprehensive production model governance framework for live trading with strict promotion, demotion, and rollback capabilities.

## Overview

The system provides:
- **Multi-metric promotion gates** with profitability, drawdown, stability, and sample size requirements
- **Champion/challenger framework** for model competition
- **Shadow trading validation** before live promotion
- **Automatic demotion triggers** on performance degradation
- **Fast and automatic rollback** with multiple strategies
- **Operator visibility dashboard** for explainability
- **Kill-switch compatibility** for emergency stops

## Architecture

### Core Components

1. **Model Governance** (`model_governance.py`)
   - Multi-metric evaluation gates
   - Promotion threshold management
   - Decision logging and audit

2. **Champion/Challenger Manager** (`champion_challenger.py`)
   - Model competition tracking
   - Automatic promotion evaluation
   - Performance comparison

3. **Shadow Trading** (`shadow_trading.py`)
   - Risk-free model validation
   - Performance tracking
   - Pre-promotion validation

4. **Automatic Demotion** (`automatic_demotion.py`)
   - Continuous health monitoring
   - Performance degradation detection
   - Automatic model retirement

5. **Rollback Manager** (`rollback_manager.py`)
   - Multiple rollback strategies
   - Fast emergency rollbacks
   - Rollback validation

6. **Kill-Switch Integration** (`kill_switch_integration.py`)
   - Emergency response coordination
   - Compatibility validation
   - Risk-aware governance

## Promotion Rules

### Multi-Metric Gates

#### 1. Profitability Gate
- **Minimum Sharpe Ratio**: 0.5
- **Minimum Win Rate**: 55%
- **Minimum Profit Factor**: 1.2
- **Maximum Acceptable Drawdown**: 15%

#### 2. Risk Gate
- **Maximum Consecutive Losses**: 10
- **Maximum Daily Loss**: 5%
- **Minimum Margin Safety**: 1.5x

#### 3. Stability Gate
- **Minimum Sample Size**: 500 trades
- **Maximum Volatility**: 25%
- **Minimum Stability Score**: 0.7

#### 4. Improvement Gate (vs Champion)
- **Minimum Sharpe Improvement**: 0.1
- **Minimum Win Rate Improvement**: 5%
- **Maximum RMSE Degradation**: 5%

### Promotion Process

1. **Candidate Registration**: Models registered as challengers
2. **Shadow Validation**: 3+ days of shadow trading required
3. **Gate Evaluation**: All gates must pass for promotion
4. **System Guard Check**: Global promotion guards must allow
5. **Automatic Promotion**: Configurable auto-promotion or manual approval

## Demotion Triggers

### Automatic Demotion Conditions

#### Performance Degradation
- **Sharpe Ratio Drop**: Below 0.3
- **Win Rate Drop**: Below 40%
- **Drawdown Limit**: Exceeds 20%

#### Risk Limits
- **Consecutive Losses**: More than 15
- **Daily Loss**: Exceeds 8%

#### Stability Issues
- **Prediction Drift**: Exceeds 15%
- **Volatility Spike**: Exceeds 40%

#### Execution Quality
- **Execution Cost**: Above 50 bps
- **Execution Quality**: Below 60%

### Demotion Actions

1. **Warning**: Initial performance issues
2. **Demote to Challenger**: Moderate degradation
3. **Rollback**: Severe degradation or critical issues
4. **Quarantine**: Model corruption or repeated failures

## Rollback Logic

### Rollback Strategies

#### 1. Immediate Rollback
- **Use Case**: Critical performance failure
- **Speed**: < 30 seconds
- **Validation**: Basic sanity checks only

#### 2. Graceful Rollback
- **Use Case**: Performance degradation
- **Speed**: < 2 minutes
- **Validation**: Basic performance validation

#### 3. Staged Rollback
- **Use Case**: Planned model changes
- **Speed**: < 5 minutes
- **Validation**: Comprehensive validation

#### 4. Emergency Rollback
- **Use Case**: Kill-switch activation
- **Speed**: < 10 seconds
- **Validation**: None (force rollback)

### Rollback Candidate Selection

1. **Most Recent Retired**: Default for immediate rollbacks
2. **Best Performing**: Selected for graceful rollbacks
3. **Highest Stability**: Selected for staged rollbacks
4. **Any Available**: Emergency rollbacks

## Operator Visibility

### Dashboard Endpoints

#### System Overview
```
GET /api/model_governance/status
```
- Model counts by stage
- Active alerts count
- Recent governance actions
- System health status

#### Model Details
```
GET /api/model_governance/model_details?model_name=X&regime=Y
```
- Complete model history
- Performance metrics
- Governance decisions
- Health monitoring data
- Rollback history

#### Promotion Queue
```
GET /api/model_governance/promotion_queue?regime=X
```
- Active challengers
- Evaluation status
- Performance metrics
- Promotion readiness

#### System Alerts
```
GET /api/model_governance/alerts?severity=CRITICAL&limit=50
```
- Active violations
- Health monitoring alerts
- Recent demotions
- Kill-switch events

### Manual Controls

#### Manual Promotion
```
POST /api/model_governance/manual_promotion
{
  "model_name": "model_x",
  "model_kind": "kind_y", 
  "model_ts_ms": 1234567890,
  "regime": "global",
  "reason": "operator_override"
}
```

#### Manual Rollback
```
POST /api/model_governance/manual_rollback
{
  "model_name": "model_x",
  "strategy": "graceful",
  "reason": "performance_concerns",
  "regime": "global"
}
```

#### Feature Toggles
```
POST /api/model_governance/toggle_feature
{
  "feature": "auto_promotion",
  "enabled": false
}
```

## Kill-Switch Compatibility

### Integration Points

1. **Pre-Action Validation**: All governance actions checked against kill-switch
2. **Emergency Response**: Automatic rollback on kill-switch activation
3. **Risk-Aware Governance**: Promotions blocked during risk-off periods
4. **Model Quarantine**: Emergency demotion capabilities

### Emergency Procedures

#### Kill-Switch Activation
1. **Automatic Detection**: Critical alerts trigger emergency response
2. **Immediate Rollback**: Emergency rollback strategy executed
3. **Model Quarantine**: Problem models isolated
4. **Operator Notification**: Alert sent to operators

#### Manual Emergency Stop
1. **Operator Action**: Manual kill-switch activation
2. **System Response**: All promotions blocked, emergency rollbacks initiated
3. **Audit Trail**: Complete action logging
4. **Recovery Plan**: Staged recovery process

## Deployment

### Installation

1. **Install Dependencies**:
```bash
pip install schedule
```

2. **Database Initialization**:
Tables are automatically created on first run.

3. **Configuration**:
Set environment variables for thresholds:
```bash
export PROMOTE_MIN_EVAL_ROWS=500
export PROMOTE_MAX_ABS_RMSE=10.0
export PROMOTION_ENABLED=1
```

### Starting the System

1. **Governance Job**:
```bash
python -m engine.strategy.jobs.model_governance_job
```

2. **API Integration**:
Add governance endpoints to your existing API server.

3. **Monitoring**:
Set up monitoring for governance job health.

### Configuration Options

#### Promotion Thresholds
```python
@dataclass
class PromotionThresholds:
    min_sharpe_ratio: float = 0.5
    min_win_rate: float = 0.55
    min_profit_factor: float = 1.2
    max_acceptable_drawdown: float = 0.15
    min_sample_size: int = 500
    max_volatility: float = 0.25
    min_stability_score: float = 0.7
```

#### Demotion Triggers
```python
@dataclass
class DemotionTriggers:
    sharpe_drop_threshold: float = 0.3
    win_rate_drop_threshold: float = 0.1
    drawdown_limit: float = 0.20
    consecutive_loss_limit: int = 15
    daily_loss_limit: float = 0.08
```

#### Rollback Configuration
```python
@dataclass
class RollbackConfig:
    auto_rollback_enabled: bool = True
    rollback_timeout_seconds: int = 30
    max_rollback_attempts: int = 3
    rollback_validation_enabled: bool = True
```

## Time Estimates

### Implementation Timeline

| Component | Development Time | Testing Time | Total |
|-----------|------------------|-------------|-------|
| Multi-metric gates | 8 hours | 4 hours | 12 hours |
| Champion/challenger | 6 hours | 3 hours | 9 hours |
| Shadow trading | 4 hours | 2 hours | 6 hours |
| Automatic demotion | 6 hours | 3 hours | 9 hours |
| Rollback system | 8 hours | 4 hours | 12 hours |
| Operator dashboard | 6 hours | 3 hours | 9 hours |
| Kill-switch integration | 4 hours | 2 hours | 6 hours |
| Orchestration job | 3 hours | 2 hours | 5 hours |
| **Total** | **45 hours** | **23 hours** | **68 hours** |

### Deployment Timeline

| Phase | Duration | Activities |
|-------|----------|------------|
| Development | 1-2 weeks | Core implementation |
| Testing | 1 week | Unit tests, integration tests |
| Staging | 3-5 days | Environment setup, validation |
| Production | 2-3 days | Gradual rollout, monitoring |

**Total Estimated Time: 2-3 weeks**

## Best Practices

### Model Lifecycle Management

1. **Gradual Promotion**: Use shadow trading extensively
2. **Conservative Thresholds**: Start with strict thresholds, relax gradually
3. **Comprehensive Monitoring**: Track all relevant metrics
4. **Regular Audits**: Review governance decisions regularly

### Operational Procedures

1. **Daily Monitoring**: Review governance dashboard
2. **Weekly Reviews**: Analyze promotion/demotion patterns
3. **Monthly Audits**: Validate system performance
4. **Quarterly Updates**: Adjust thresholds based on performance

### Emergency Preparedness

1. **Rollback Candidates**: Always maintain qualified rollback candidates
2. **Communication Plans**: Clear escalation procedures
3. **Testing**: Regular emergency drill testing
4. **Documentation**: Keep procedures up to date

## Troubleshooting

### Common Issues

#### Promotion Not Working
1. Check system-wide promotion guard status
2. Verify all gate thresholds are met
3. Ensure shadow validation is complete
4. Check kill-switch compatibility

#### Rollback Failures
1. Verify rollback candidates exist
2. Check database connectivity
3. Validate model integrity
4. Review rollback logs

#### Performance Issues
1. Monitor governance job execution time
2. Check database query performance
3. Review log volumes
4. Optimize scheduling intervals

### Monitoring

#### Key Metrics
- Governance job execution time
- Promotion/demotion frequency
- Rollback success rate
- Alert volume and severity

#### Alerts
- Governance job failures
- Promotion guard blocks
- Rollback failures
- Kill-switch activations

This system provides a robust, production-ready framework for model governance that ensures only the best models trade while maintaining safety and operational visibility.
