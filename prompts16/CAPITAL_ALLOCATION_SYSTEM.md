# Capital Allocation Engine

## Overview
A sophisticated capital allocation system that distributes $500k across strategies, assets, and horizons while managing risk and execution constraints.

## Key Features

### 1. Multi-Dimensional Allocation
- **Strategies**: Momentum, mean reversion, volatility, macro
- **Assets**: Equities across sectors (tech, finance, energy, consumer)
- **Horizons**: 5m, 1h, 1d, 3d, 2w

### 2. Risk Management
- **Correlation Penalties**: Reduces clustered exposures
- **Capacity Limits**: Enforces execution constraints
- **Risk Gates**: Portfolio-level risk controls
- **Drawdown Protection**: Capital preservation mode

### 3. Adaptive Rebalancing
- **Performance-Based**: Adjusts allocation based on strategy performance
- **Volatility-Adjusted**: Dynamic rebalancing frequency
- **Turnover Limits**: Prevents overtrading

## Architecture

### Core Components
1. **CapitalAllocationEngine**: Main allocation logic
2. **CorrelationRiskAnalyzer**: Risk penalty calculations
3. **ExecutionCapacityManager**: Order flow constraints
4. **AdaptiveRebalancer**: Performance-based rebalancing
5. **RiskConstraints**: Risk limit definitions
6. **ExecutionIntegration**: Broker layer connectivity

### Data Flow
```
Alerts → Allocation Engine → Risk Analysis → Capacity Check → Orders → Execution
```

## Configuration

### Environment Variables
```bash
# Risk constraints
PORTFOLIO_GROSS_CAP=1.0
PORTFOLIO_MAX_NET_EXPOSURE=0.6
MAX_POSITION_FRACTION=0.20

# Rebalancing
MIN_REBALANCE_INTERVAL_S=300
MAX_TURNOVER=0.4

# Execution
MAX_ORDERS_PER_MINUTE=50
MAX_NOTIONAL_PER_MINUTE=50000.0
```

### Risk Profiles
- **Conservative**: Lower exposure, tighter limits
- **Default**: Balanced risk/return
- **Aggressive**: Higher exposure, relaxed limits

## Usage

### Running the Engine
```bash
python -m engine.strategy.capital_allocation_main
```

### Single Allocation
```python
from engine.strategy.capital_allocation_engine import run_capital_allocation
targets = run_capital_allocation()
```

## Performance Monitoring

### Key Metrics
- **Total Allocation**: Sum of all position weights
- **Correlation Risk**: Portfolio correlation score
- **Turnover**: Rebalancing activity level
- **Execution Quality**: Cost and latency metrics

### Alerts
- High correlation risk (>0.3)
- Capacity limit breaches
- Performance degradation
- Risk constraint violations

## Integration Points

### Existing Systems
- **Portfolio Management**: `portfolio.py`
- **Risk Gates**: `portfolio_risk_gate.py`
- **Position Sizing**: `position_sizing.py`
- **Execution Layer**: `broker_router.py`

### Database Tables
- `alerts`: Input signals
- `positions`: Current allocations
- `orders`: Execution records
- `performance`: Strategy metrics

## Deployment

### Prerequisites
- SQLite database with alerts table
- Execution broker connection
- Risk constraint configuration

### Monitoring
- Log allocation decisions
- Track performance metrics
- Alert on constraint violations
- Monitor execution quality

## Future Enhancements
- Machine learning-based allocation
- Real-time correlation updates
- Advanced execution optimization
- Multi-asset class support
