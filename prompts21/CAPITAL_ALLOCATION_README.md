# Capital Allocation Engine Implementation

## Overview

This implementation provides a comprehensive capital allocation engine that meets all the specified requirements:

- **Multi-strategy, multi-asset, multi-horizon allocation**
- **Correlation-based risk penalties**
- **Execution capacity limits**
- **Adaptive performance-based rebalancing**
- **Stable allocation with overtrading protection**

## Architecture

### Core Components

1. **CapitalAllocationEngine** (`engine/strategy/capital_allocation_engine.py`)
   - Main allocation algorithm
   - Correlation penalty calculations
   - Performance-based scoring
   - Capacity limit enforcement

2. **RebalancingManager** (`engine/strategy/rebalancing_manager.py`)
   - Rebalancing cadence management
   - Overtrading protection
   - Execution layer integration
   - Volatility-based timing adjustments

3. **CapitalAllocationJob** (`engine/strategy/capital_allocation_job.py`)
   - Job system integration
   - Database schema management
   - Performance tracking
   - Status monitoring

4. **API Layer** (`engine/strategy/api_capital_allocation.py`)
   - REST API endpoints
   - Status monitoring
   - Manual rebalancing triggers

## Key Features

### 1. Allocation Algorithm

- **Signal Processing**: Uses confidence-weighted z-scores from alerts
- **Opportunity Weighting**: Convex optimization via `opportunity_weight()` function
- **Performance Adaptation**: Strategy performance scores adjust allocations
- **Risk Constraints**: Maximum position sizes, gross exposure limits

### 2. Correlation Risk Penalties

- **Historical Correlations**: Uses asset returns from last 30 days
- **Dynamic Penalties**: Proportional to correlation and position size
- **Matrix Updates**: Efficient correlation matrix calculation
- **Penalty Capping**: Maximum 30% penalty per position

### 3. Execution Capacity Limits

- **Strategy-Asset Limits**: Per-strategy, per-asset daily capacity
- **Dynamic Adjustment**: Real-time capacity usage tracking
- **Constraint Enforcement**: Hard limits on allocation sizes
- **Capacity Monitoring**: Database tracking of usage vs limits

### 4. Adaptive Performance Allocation

- **Performance History**: Last 50 observations per strategy
- **Sharpe-like Scoring**: Return/risk ratio with volatility floor
- **Dynamic Weights**: Performance multipliers adjust allocations
- **Stability Bounds**: Score clamping prevents extreme adjustments

### 5. Rebalancing Cadence

- **Base Intervals**: 5-minute default rebalancing
- **Volatility Scaling**: VIX-based interval adjustments
- **Overtrading Protection**: Turnover-based interval penalties
- **Minimum Hold Times**: 30-minute position age requirements

## Configuration

### Environment Variables

```bash
# Capital Allocation
CAPITAL_ALLOCATION_ENABLED=1
CAPITAL_ALLOCATION_INTERVAL_S=300
CAPITAL_ALLOCATION_DRY_RUN=0

# Portfolio Constraints
PORTFOLIO_MAX_POSITIONS=3
PORTFOLIO_GROSS_CAP=1.0
PORTFOLIO_MAX_W_PER_SYMBOL=0.45

# Risk Controls
PORTFOLIO_CORR_LOOKBACK=240
PORTFOLIO_CORR_MAX=0.92
PORTFOLIO_MIN_HOLD_S=1800
```

### Database Schema

The system creates several tables:

- `capital_allocation_state`: Allocation history and metadata
- `execution_capacity`: Strategy-asset capacity limits
- `asset_returns`: Historical returns for correlation calculations
- `strategy_performance`: Strategy performance metrics

## API Endpoints

### GET /api/capital-allocation/status
Returns current allocation status:
```json
{
  "status": "active",
  "last_allocation_ts": 1640995200000,
  "total_capital": 500000.0,
  "allocated_capital": 350000.0,
  "allocation_count": 5,
  "correlation_penalty_avg": 0.12,
  "execution_capacity_used": 0.65
}
```

### POST /api/capital-allocation/run
Triggers manual allocation run:
```json
{
  "ok": true,
  "status": "allocation_completed",
  "targets_count": 5,
  "execution_result": {...}
}
```

### GET /api/capital-allocation/metrics?days=30
Returns performance metrics:
```json
{
  "status": "success",
  "period_days": 30,
  "total_allocations": 1440,
  "avg_allocated_capital": 325000.0,
  "avg_position_count": 4.2,
  "allocation_stability": 0.85
}
```

## Integration with Existing System

### Job System Integration

The capital allocation job is registered in `engine/runtime/job_registry.py`:

```python
"capital_allocation": ("engine/strategy/capital_allocation_job.py", "daemon", None, {"execution": True})
```

### Execution Layer Integration

Uses the existing broker router via `apply_new_portfolio_orders_router()`:

```python
result = apply_new_portfolio_orders_router(
    dry_run=False,
    override_orders=broker_orders
)
```

### Portfolio Integration

Extends existing portfolio system in `engine/strategy/portfolio.py` with:

- Enhanced correlation handling
- Capacity limit enforcement
- Performance-based allocation adjustments

## Usage Examples

### Manual Rebalancing

```python
from engine.strategy.rebalancing_manager import run_rebalancing_cycle

# Run a single rebalance (dry run)
result = run_rebalancing_cycle(dry_run=True)
print(result)
```

### Status Monitoring

```python
from engine.strategy.capital_allocation_job import get_capital_allocation_status

status = get_capital_allocation_status()
print(f"Allocated: ${status['allocated_capital']:,.2f}")
print(f"Positions: {status['allocation_count']}")
```

### Performance Updates

```python
from engine.strategy.capital_allocation_job import update_strategy_performance

# Update strategy performance
update_strategy_performance("momentum", {
    "sharpe_ratio": 1.2,
    "total_return": 0.15,
    "max_drawdown": 0.08,
    "win_rate": 0.62
})
```

## Risk Management

### Correlation Risk

- **Matrix Calculation**: Daily returns over configurable lookback
- **Penalty Formula**: `penalty = |corr| * other_weight * 0.5`
- **Maximum Penalty**: Capped at 30% per position
- **Update Frequency**: Recalculated each rebalance cycle

### Capacity Risk

- **Daily Limits**: Strategy-asset specific capacity constraints
- **Usage Tracking**: Real-time monitoring of daily usage
- **Hard Enforcement**: Allocations capped by available capacity
- **Reset Mechanism**: Daily capacity reset at midnight

### Overtrading Protection

- **Turnover Thresholds**: 30% turnover triggers interval scaling
- **Minimum Hold Times**: 30 minutes before position reversal
- **Volatility Scaling**: High volatility extends rebalance intervals
- **Position Age Filters**: Prevents excessive churn

## Monitoring and Alerting

### Key Metrics

- **Allocation Efficiency**: `allocated_capital / total_capital`
- **Correlation Penalty Average**: Portfolio diversification metric
- **Turnover Rate**: Trading activity indicator
- **Position Count**: Portfolio concentration metric

### Alert Conditions

- **High Correlation**: Average penalty > 20%
- **Low Allocation**: Efficiency < 50%
- **Excessive Turnover**: Turnover > 40%
- **Capacity Constraints**: Usage > 90% of limits

## Future Enhancements

### Planned Features

1. **Advanced Correlation Models**: Dynamic conditional correlations
2. **Liquidity-Aware Sizing**: Market depth integration
3. **Regime Detection**: Market state-aware allocation
4. **Multi-Period Optimization**: Forward-looking allocation models
5. **Risk Budgeting**: Portfolio-level risk constraints

### Extensibility

The system is designed for easy extension:

- **New Strategies**: Add to performance tracking
- **Additional Risk Metrics**: Extend correlation calculations
- **Custom Constraints**: Add new allocation rules
- **Alternative Execution**: Support additional brokers

## Conclusion

This capital allocation engine provides a robust, production-ready solution that meets all specified requirements while maintaining flexibility for future enhancements. The modular design allows for easy testing, monitoring, and extension of individual components.
