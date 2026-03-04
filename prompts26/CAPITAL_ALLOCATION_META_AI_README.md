# Capital Allocation Meta-AI System

## Overview

The Capital Allocation Meta-AI is a sophisticated portfolio-level intelligence system that dynamically allocates capital across trading strategies using multi-objective optimization and explainable AI. It sits on top of existing strategies and provides cross-strategy capital intelligence.

## Architecture

### Core Components

1. **CapitalAllocationMetaAI** (`engine/strategy/capital_allocation_meta_ai.py`)
   - Main Meta-AI optimization engine
   - Multi-objective optimization (return vs risk vs correlation vs stability)
   - Explainable allocation decisions
   - Confidence-based gating

2. **Enhanced CapitalAllocationEngine** (`engine/strategy/capital_allocation_engine.py`)
   - Integrated Meta-AI capabilities
   - Safe fallback to traditional allocation
   - Risk controls and constraints enforcement

3. **Meta-AI API** (`engine/strategy/api_capital_allocation_meta.py`)
   - REST endpoints for monitoring and control
   - Manual allocation triggers
   - Performance updates and diagnostics

## Key Features

### 1. Multi-Objective Optimization

The Meta-AI optimizes across four objectives:

- **Return Maximization** (40% weight): Expected portfolio returns
- **Risk Minimization** (30% weight): Portfolio volatility and drawdowns  
- **Correlation Management** (20% weight): Diversification benefits
- **Allocation Stability** (10% weight): Minimize unnecessary turnover

### 2. Comprehensive Input Analysis

**Strategy Metrics:**
- Returns at multiple horizons (1d, 5d, 30d)
- Volatility and Sharpe ratio
- Maximum and current drawdowns
- Win rates and performance scores
- Confidence scores based on data quality

**Risk Analysis:**
- Strategy correlation matrix
- Portfolio correlation contributions
- Regime compatibility scores
- Concentration risk metrics

### 3. Explainable AI Decisions

Every allocation decision includes:

- **Primary Reasoning**: Why the allocation changed
- **Risk Factors**: Correlation penalties, drawdown impacts
- **Performance Factors**: Strategy performance, regime compatibility
- **Confidence Scoring**: Data quality and stability metrics
- **Expected Impact**: Return and risk contributions

### 4. Safety Constraints

**Confidence Gating:**
- Minimum confidence threshold (60%)
- Low-confidence strategies get limited allocation
- Data quality affects confidence scores

**Risk Limits:**
- Maximum position size per strategy (40%)
- Maximum single-strategy drawdown (15%)
- Maximum portfolio correlation (70%)
- Minimum strategy count (2)

**Safe Degradation:**
- Automatic fallback to traditional allocation
- Hard limits on allocation changes
- Overtrading protection

## Installation & Setup

### Environment Variables

```bash
# Meta-AI Configuration
META_AI_ENABLED=1                    # Enable/disable Meta-AI
CAPITAL_ALLOCATION_INTERVAL_S=300     # Allocation interval (5 minutes)
CAPITAL_ALLOCATION_DRY_RUN=0          # 0=live, 1=dry-run

# Portfolio Constraints  
PORTFOLIO_MAX_POSITIONS=3             # Max concurrent positions
PORTFOLIO_GROSS_CAP=1.0               # Gross exposure cap
PORTFOLIO_MAX_W_PER_SYMBOL=0.45       # Max weight per symbol

# Risk Controls
PORTFOLIO_CORR_LOOKBACK=240           # Correlation lookback (days)
PORTFOLIO_CORR_MAX=0.92               # Max correlation
PORTFOLIO_MIN_HOLD_S=1800             # Minimum hold time (30 min)
```

### Database Schema

The system uses these tables:

- `strategy_performance`: Strategy metrics and history
- `strategy_daily_returns`: Daily returns for correlation calculations
- `capital_allocation_state`: Allocation tracking
- `alerts`: Trading signals and opportunities

## Usage Examples

### 1. Manual Allocation

```python
from engine.strategy.capital_allocation_meta_ai import run_meta_allocation

result = run_meta_allocation(
    strategies=['momentum', 'mean_reversion', 'trend_following'],
    current_weights={'momentum': 0.4, 'mean_reversion': 0.3, 'trend_following': 0.3},
    total_capital=500000.0
)

if result['success']:
    print(f"Optimal weights: {result['optimal_weights']}")
    print(f"Diagnostics: {result['diagnostics']}")
```

### 2. API Integration

```python
# Get Meta-AI status
response = requests.get('/api/capital-allocation-meta/status')
status = response.json()

# Run manual allocation
request = {
    "strategies": ["momentum", "mean_reversion"],
    "total_capital": 500000.0
}
response = requests.post('/api/capital-allocation-meta/allocate', json=request)
allocation = response.json()
```

### 3. Performance Updates

```python
from engine.strategy.capital_allocation_job import update_strategy_performance

update_strategy_performance("momentum", {
    "sharpe_ratio": 1.2,
    "total_return": 0.15,
    "max_drawdown": 0.08,
    "win_rate": 0.62
})
```

## API Endpoints

### GET /api/capital-allocation-meta/status
Get Meta-AI system status and health metrics.

### POST /api/capital-allocation-meta/allocate
Run manual capital allocation using Meta-AI optimization.

**Request:**
```json
{
    "strategies": ["momentum", "mean_reversion"],
    "current_weights": {"momentum": 0.6, "mean_reversion": 0.4},
    "total_capital": 500000.0,
    "force_allocation": false
}
```

### POST /api/capital-allocation-meta/optimize-config
Update optimization objective weights.

**Request:**
```json
{
    "return_weight": 0.4,
    "risk_weight": 0.3,
    "correlation_weight": 0.2,
    "stability_weight": 0.1
}
```

### POST /api/capital-allocation-meta/performance
Update strategy performance metrics.

### GET /api/capital-allocation-meta/diagnostics
Get detailed allocation diagnostics and history.

### GET /api/capital-allocation-meta/strategies
List available strategies with performance metrics.

### POST /api/capital-allocation-meta/toggle
Enable or disable Meta-AI allocation.

### GET /api/capital-allocation-meta/health
System health check.

## Allocation Decision Process

### 1. Data Collection
- Gather strategy performance metrics
- Calculate correlation matrix
- Assess regime compatibility
- Evaluate confidence scores

### 2. Optimization
- Multi-objective function optimization
- Constraint satisfaction (weights sum to 1, bounds)
- Risk-adjusted return maximization
- Correlation penalty application

### 3. Decision Generation
- Calculate optimal weights
- Generate allocation decisions
- Create explainable reasoning
- Apply confidence gating

### 4. Risk Control
- Enforce position limits
- Apply correlation penalties
- Check concentration risk
- Validate allocation stability

## Diagnostics & Monitoring

### Key Metrics

- **Allocation Efficiency**: Capital utilization rate
- **Diversification Score**: Correlation-based diversification metric  
- **Confidence Distribution**: High/medium/low confidence allocations
- **Turnover Rate**: Trading activity indicator
- **Risk Contribution**: Strategy-level risk impacts

### Alert Conditions

- High correlation penalty (>20%)
- Low allocation efficiency (<50%)
- Excessive turnover (>40%)
- Low confidence allocations dominant
- Strategy performance degradation

### Historical Analysis

The system tracks:
- Allocation decisions with full reasoning
- Performance vs expectations
- Risk metric evolution
- Regime adaptation effectiveness

## Risk Management

### Correlation Risk
- Dynamic correlation matrix updates
- Penalty proportional to correlation exposure
- Maximum 30% penalty per position
- Portfolio-level correlation limits

### Concentration Risk
- Per-strategy allocation caps
- Asset-level concentration limits
- Horizon diversification requirements
- Real-time concentration monitoring

### Performance Risk
- Strategy performance degradation detection
- Automatic allocation reduction for poor performers
- Confidence-based allocation limits
- Performance score clamping

### Operational Risk
- Safe fallback mechanisms
- Data quality validation
- System health monitoring
- Manual override capabilities

## Integration Guidelines

### 1. Gradual Rollout
Start with dry-run mode to validate allocations before going live.

### 2. Performance Monitoring
Track allocation effectiveness vs traditional methods.

### 3. Risk Limit Adjustments
Fine-tune constraints based on portfolio characteristics.

### 4. Regime Adaptation
Monitor regime compatibility and adjust as needed.

## Troubleshooting

### Common Issues

**Meta-AI Disabled**
- Check `META_AI_ENABLED` environment variable
- Verify system health via `/health` endpoint

**Poor Allocation Quality**
- Review strategy performance data quality
- Check correlation matrix calculations
- Verify confidence scoring logic

**High Turnover**
- Increase stability weight in optimization
- Adjust minimum hold time constraints
- Review rebalancing frequency

**Low Confidence**
- Improve data quality and recency
- Adjust confidence thresholds
- Review performance calculation methods

### Debug Mode

Enable detailed logging:
```python
import logging
logging.getLogger('engine.strategy.capital_allocation_meta_ai').setLevel(logging.DEBUG)
```

## Future Enhancements

### Planned Features
1. **Advanced Correlation Models**: Dynamic conditional correlations
2. **Liquidity-Aware Sizing**: Market depth integration  
3. **Multi-Period Optimization**: Forward-looking models
4. **Risk Budgeting**: Portfolio-level risk constraints
5. **Regime Prediction**: Proactive regime adaptation

### Extensibility
The system supports:
- Custom objective functions
- Additional risk metrics
- Alternative optimization methods
- New constraint types
- Custom diagnostic metrics

## Conclusion

The Capital Allocation Meta-AI provides a robust, production-ready solution for intelligent cross-strategy capital allocation. It balances optimization sophistication with safety constraints, providing explainable decisions while maintaining system reliability.

The modular design allows for easy testing, monitoring, and extension of individual components, ensuring the system can evolve with changing requirements and market conditions.
