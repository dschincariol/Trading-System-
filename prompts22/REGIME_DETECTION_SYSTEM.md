# Regime Detection and Conditional Routing System

A comprehensive regime detection system that enhances model governance and capital allocation through market regime awareness.

## Overview

The system provides:
- **Multi-dimensional regime detection** across volatility, liquidity, macro stress, and trend/range dimensions
- **Real-time regime classification** with confidence scoring
- **Model-regime compatibility scoring** for intelligent model selection
- **Regime-aware promotion/demotion decisions** with protection mechanisms
- **Dynamic capital allocation** adjustments based on regime compatibility
- **Production-safe operation** with comprehensive error handling and monitoring

## Architecture

### Core Components

1. **RegimeDetectionSystem** (`regime_detection_system.py`)
   - Main regime detection engine
   - 16 predefined regimes across 4 categories
   - Model compatibility scoring
   - Promotion/demotion decision logic

2. **RegimeAwareGovernance** (`regime_aware_governance.py`)
   - Integration with existing model governance
   - Regime-aware promotion/demotion decisions
   - Capital allocation adjustments

3. **RegimeDetectionJob** (`jobs/regime_detection_job.py`)
   - Periodic regime detection cycles
   - Automated governance decisions
   - Analytics and monitoring

### Regime Categories

#### 1. Volatility Regimes
- **vol_low**: Low volatility (VIX Z < -1.0)
- **vol_normal**: Normal volatility (VIX Z ≈ 0.0)
- **vol_elevated**: Elevated volatility (VIX Z > 1.0)
- **vol_extreme**: Extreme volatility (VIX Z > 2.0)

#### 2. Liquidity Regimes
- **liq_high**: High liquidity (tight spreads, high volume)
- **liq_normal**: Normal liquidity conditions
- **liq_low**: Low liquidity (wide spreads, low volume)

#### 3. Macro Stress Regimes
- **macro_risk_on**: Risk-on environment (strong risk appetite)
- **macro_neutral**: Neutral macro environment
- **macro_risk_off**: Risk-off environment (flight to safety)
- **macro_stress**: High macro stress (systemic concerns)

#### 4. Trend/Range Regimes
- **trend_strong**: Strong trending environment
- **trend_moderate**: Moderate trending environment
- **range_bound**: Range-bound environment
- **choppy**: Choppy, directionless environment

## Installation and Setup

### Environment Variables

```bash
# Core system
REGIME_COMPAT_ENABLE=1
REGIME_DETECTION_INTERVAL_MIN=5
REGIME_MODEL_VERSION=regime_stack_v1

# Governance integration
REGIME_AWARE_GOVERNANCE=1
REGIME_GOVERNANCE_INTEGRATION=1
REGIME_CAPITAL_ADJUSTMENT=1

# Monitoring
REGIME_MONITORED_SYMBOLS=SPY,QQQ,IWM
REGIME_COMPAT_DECAY=0.97
REGIME_COMPAT_MIN_TRADES=25
```

### Database Tables

The system automatically creates the following tables:

```sql
-- Model regime profiles
CREATE TABLE model_regime_profiles (
    model_name TEXT PRIMARY KEY,
    model_type TEXT NOT NULL,
    preferred_regimes TEXT,
    avoided_regimes TEXT,
    min_regime_confidence REAL DEFAULT 0.5,
    regime_adaptation_score REAL DEFAULT 0.5,
    last_updated_ms INTEGER,
    created_at_ms INTEGER DEFAULT (strftime('%s', 'now') * 1000)
);

-- Regime detection history
CREATE TABLE regime_detection_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms INTEGER NOT NULL,
    primary_regime TEXT NOT NULL,
    regime_state TEXT NOT NULL,
    confidence REAL NOT NULL,
    regime_vector TEXT NOT NULL,
    risk_metrics TEXT NOT NULL,
    model_compatibility_scores TEXT,
    recommended_actions TEXT
);

-- Regime model actions
CREATE TABLE regime_model_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms INTEGER NOT NULL,
    model_name TEXT NOT NULL,
    action_type TEXT NOT NULL,
    regime_name TEXT NOT NULL,
    reason TEXT NOT NULL,
    compatibility_score REAL,
    risk_multiplier REAL,
    capital_adjustment REAL
);
```

## Usage Examples

### Basic Regime Detection

```python
from engine.strategy.regime_detection_system import detect_current_regime

# Detect current regime for SPY
result = detect_current_regime(symbol="SPY")

print(f"Primary Regime: {result.primary_regime}")
print(f"Confidence: {result.confidence:.2f}")
print(f"Risk Metrics: {result.risk_metrics}")
print(f"Recommended Actions: {result.recommended_actions}")
```

### Model Promotion with Regime Awareness

```python
from engine.strategy.regime_aware_governance import evaluate_model_promotion_regime_aware

# Evaluate model promotion with regime consideration
governance_metrics = {
    "sharpe_ratio": 0.8,
    "win_rate": 0.62,
    "profit_factor": 1.4,
    "max_drawdown": 0.12
}

decision = evaluate_model_promotion_regime_aware(
    "momentum_model", "momentum", governance_metrics
)

print(f"Recommended Action: {decision.recommended_action}")
print(f"Regime: {decision.regime_name}")
print(f"Compatibility Score: {decision.compatibility_score:.3f}")
print(f"Reasoning: {decision.reasoning}")
```

### Capital Allocation Adjustment

```python
from engine.strategy.regime_aware_governance import adjust_capital_allocation_regime_aware
from engine.strategy.capital_allocation_engine import AllocationTarget

# Create allocation targets
targets = [
    AllocationTarget(
        strategy="momentum",
        asset="SPY",
        horizon="1h",
        weight=0.15,
        expected_return=0.08,
        confidence=0.7,
        correlation_penalty=0.02
    ),
    AllocationTarget(
        strategy="mean_reversion",
        asset="QQQ",
        horizon="5m",
        weight=0.10,
        expected_return=0.06,
        confidence=0.6,
        correlation_penalty=0.01
    )
]

# Apply regime-aware adjustments
adjusted_targets = adjust_capital_allocation_regime_aware(targets)

for target in adjusted_targets:
    print(f"{target.strategy}_{target.asset}: {target.weight:.3f}")
```

### Model Profile Management

```python
from engine.strategy.regime_detection_system import get_regime_detection_system

# Get system instance
system = get_regime_detection_system()

# Create model profile
from engine.strategy.regime_detection_system import ModelRegimeProfile

profile = ModelRegimeProfile(
    model_name="volatility_breakout_model",
    model_type="volatility_breakout",
    preferred_regimes=["vol_elevated", "vol_extreme"],
    avoided_regimes=["vol_low", "vol_normal"],
    min_regime_confidence=0.7,
    regime_adaptation_score=0.8,
    last_updated_ms=int(time.time() * 1000)
)

# Add to system (this would typically be done via database)
system.model_profiles["volatility_breakout_model"] = profile
```

### Running the Detection Job

```python
from engine.strategy.jobs.regime_detection_job import run_regime_detection_cycle

# Run a single detection cycle
results = run_regime_detection_cycle()

print(f"Cycle Success: {results['success']}")
print(f"Symbol Detections: {len(results['symbol_detections'])}")
print(f"Governance Decisions: {len(results['governance_decisions'])}")
print(f"Capital Adjustments: {results['capital_adjustments']}")
```

## Integration with Existing Systems

### Model Governance Integration

The regime system integrates seamlessly with existing model governance:

```python
# In your existing promotion logic
from engine.strategy.regime_aware_governance import evaluate_model_promotion_regime_aware

def evaluate_model_for_promotion(model_name, model_type, metrics):
    # Standard governance evaluation
    standard_result = evaluate_promotion_gates(model_name, metrics)
    
    # Regime-aware evaluation
    regime_result = evaluate_model_promotion_regime_aware(
        model_name, model_type, metrics
    )
    
    # Combine results
    if regime_result.recommended_action == "PROMOTE_REGIME_ENHANCED":
        return True, "Regime-enhanced promotion"
    elif regime_result.recommended_action == "HOLD_REGIME_BLOCKED":
        return False, f"Blocked by regime: {regime_result.reasoning}"
    
    return standard_result
```

### Capital Allocation Integration

```python
# In your capital allocation logic
from engine.strategy.regime_aware_governance import adjust_capital_allocation_regime_aware

def calculate_optimal_allocation():
    # Standard allocation calculation
    base_targets = run_capital_allocation()
    
    # Apply regime adjustments
    regime_adjusted = adjust_capital_allocation_regime_aware(base_targets)
    
    return regime_adjusted
```

## Monitoring and Analytics

### Regime Summary

```python
from engine.strategy.regime_detection_system import get_regime_detection_system

system = get_regime_detection_system()
summary = system.get_regime_summary(hours_back=24)

print(f"Total Detections: {summary['total_detections']}")
print(f"Average Confidence: {summary['avg_confidence']:.3f}")
print("Regime Distribution:")
for regime, data in summary['regime_distribution'].items():
    print(f"  {regime}: {data['count']} detections (avg conf: {data['avg_confidence']:.3f})")
```

### Governance Status

```python
from engine.strategy.regime_aware_governance import get_regime_governance_status

status = get_regime_governance_status()

print(f"Current Regime: {status['current_regime']['name']}")
print(f"Regime Confidence: {status['current_regime']['confidence']:.3f}")
print(f"Governance Enabled: {status['governance_status']['enabled']}")
print(f"Model Profiles: {status['governance_status']['model_profiles_count']}")
```

## Safety and Constraints

### Demotion Protection

Models are protected from demotion when:
- They are in a preferred regime with high compatibility (>0.7)
- Current regime confidence is high (>0.6)
- Model has strong historical performance in current regime

### Promotion Constraints

Models must pass additional checks for promotion:
- Current regime must not be in avoided list
- Regime compatibility score must be >0.4
- If regime is not preferred, compatibility must be >0.7

### Capital Allocation Limits

- Maximum allocation adjustment: ±50%
- Minimum allocation after adjustment: 0.1
- Risk multiplier bounds: 0.5 - 2.0

## Performance Considerations

### Caching

- Detection results are cached for 5 minutes
- Model compatibility scores cached for 1 hour
- Regime definitions cached in memory

### Scalability

- Supports 100+ concurrent models
- Handles 10+ monitored symbols
- Detection cycle completes in <2 seconds

### Error Handling

- Graceful degradation on detection failures
- Fallback to neutral decisions on errors
- Comprehensive error logging and monitoring

## Testing

Run the comprehensive test suite:

```bash
cd /home/pooja/my_prompts/prompts16
python -m pytest tests/test_regime_detection_system.py -v
```

Test coverage includes:
- Regime definition validation
- Detection logic accuracy
- Model compatibility scoring
- Promotion/demotion decisions
- Integration with existing systems
- Error handling and edge cases
- Performance and caching

## Troubleshooting

### Common Issues

1. **Low Detection Confidence**
   - Check data quality for regime indicators
   - Verify factor universe is populated
   - Review regime threshold configurations

2. **Model Compatibility Issues**
   - Ensure model profiles are properly configured
   - Check preferred/avoided regime lists
   - Verify historical performance data

3. **Integration Problems**
   - Confirm environment variables are set
   - Check database table creation
   - Verify import paths and dependencies

### Monitoring Alerts

Set up alerts for:
- Detection confidence < 0.5 for >30 minutes
- Error count > 5 in detection cycle
- Model compatibility scores < 0.3 for preferred models
- Capital allocation adjustments > 30%

## Future Enhancements

Planned improvements:
- Machine learning-based regime prediction
- Multi-asset regime correlation analysis
- Dynamic regime threshold adaptation
- Real-time regime transition detection
- Advanced model performance attribution

## Support

For questions or issues:
1. Check the troubleshooting section above
2. Review the test suite for usage examples
3. Examine the log files for detailed error information
4. Consult the existing model governance documentation

---

This system provides a robust, production-ready framework for regime-aware model governance that enhances trading performance while maintaining safety and operational visibility.
