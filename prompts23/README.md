# Research Budget Allocation System

A comprehensive, deterministic, and explainable system for allocating research resources based on expected payoff, balancing exploration vs exploitation, and automatically stopping unproductive research paths.

## Overview

This system addresses the challenge of allocating finite research resources across multiple assets, models, and horizons while continuously training and validating. It provides automated decision-making with full explainability and safety constraints.

## Key Features

### 1. **Smart Allocation Logic**
- Expected payoff calculations with confidence scoring
- Multiple allocation strategies (balanced, performance-focused, exploration-focused)
- Risk-adjusted allocation decisions

### 2. **Exploration vs Exploitation Balance**
- Multiple exploration strategies (UCB, Thompson Sampling, Epsilon-Greedy, Bayesian Optimization)
- Adaptive exploration parameters
- Automatic strategy switching based on performance

### 3. **Automatic Stop Criteria**
- Performance threshold monitoring
- ROI-based stopping
- Statistical significance testing
- Trend analysis and convergence detection
- Resource exhaustion monitoring

### 4. **Dynamic Budget Reallocation**
- Portfolio optimization approaches
- Opportunity cost analysis
- Automatic reallocation from stopped/unproductive assets
- Multiple reallocation strategies

### 5. **Comprehensive Monitoring**
- Real-time feedback signals
- Performance tracking and anomaly detection
- Alert system with multiple severity levels
- Metrics collection and analysis

### 6. **Safety Constraints**
- Deterministic behavior guarantees
- Budget limits and allocation caps
- Concentration risk management
- Resource usage constraints
- Full audit trail

### 7. **Explainable Reporting**
- Detailed decision explanations
- Interactive visualizations
- Multiple report types (executive, technical, compliance)
- HTML and JSON export options

## Architecture

The system consists of 7 main components:

1. **Core Allocator** (`research_budget_allocator.py`) - Main allocation logic
2. **Exploration Manager** (`exploration_exploitation_manager.py`) - Exploration strategies
3. **Stop Criteria Manager** (`stop_criteria_manager.py`) - Automatic stopping logic
4. **Budget Reallocator** (`budget_reallocator.py`) - Dynamic reallocation
5. **Feedback Monitor** (`feedback_monitor.py`) - Monitoring and alerting
6. **Safety Constraints** (`safety_constraints.py`) - Safety and determinism
7. **Reporting System** (`explainable_reporting.py`) - Explanations and visualizations

## Quick Start

### Installation

```bash
# Install required dependencies
pip install numpy pandas scipy matplotlib seaborn

# All system files are self-contained Python modules
```

### Basic Usage

```python
from main_system import ResearchBudgetAllocationSystem

# Initialize the system with $1M budget
system = ResearchBudgetAllocationSystem(total_budget=1000000)

# Add research assets
system.add_research_asset("gpt4_finetune", "GPT-4 Fine-tuning", "language_model", 200000)
system.add_research_asset("vision_transformer", "Vision Transformer", "computer_vision", 150000)
system.add_research_asset("rl_agent", "Reinforcement Learning", "rl_agent", 100000)

# Run allocation cycle
results = system.run_allocation_cycle()
print(f"Allocated ${results['total_allocated']:,.2f} across {results['allocation_decisions']} assets")

# Record performance updates
system.record_performance_update("gpt4_finetune", 0.8, 50000, 100, 1000)

# Generate reports
report_path = system.generate_system_report()
print(f"Report generated: {report_path}")
```

## Configuration

The system can be configured with various parameters:

```python
config = {
    'allocation_strategy': 'BALANCED',  # EXPECTED_PAYOFF, EXPLORATION_BONUS, BALANCED
    'exploration_strategy': 'ADAPTIVE_UCB',  # UCB, THOMPSON_SAMPLING, EPSILON_GREEDLY, etc.
    'reallocation_strategy': 'PORTFOLIO_OPTIMIZATION',
    'deterministic_level': 'FULL',  # FULL, PREDICTABLE, BOUNDED, FLEXIBLE
    'auto_reallocate': True,
    'auto_stop': True,
    'monitoring_window_size': 100,
    'alert_threshold': 2.0
}

system = ResearchBudgetAllocationSystem(total_budget=1000000, config=config)
```

## Advanced Features

### Custom Exploration Strategies

```python
from exploration_exploitation_manager import ExplorationStrategyBase

class CustomExplorationStrategy(ExplorationStrategyBase):
    def calculate_exploration_bonus(self, asset, total_assets, total_trials):
        # Custom logic here
        return score, reasoning
    
    def update_strategy_metrics(self, asset, performance):
        # Update strategy-specific metrics
        pass

# Register custom strategy
exploration_manager = ExplorationExploitationManager()
exploration_manager.strategy = CustomExplorationStrategy()
```

### Custom Stop Criteria

```python
def custom_stop_criterion(asset, criterion):
    # Custom stopping logic
    if some_condition:
        return StopDecision(
            asset_id=asset.id,
            should_stop=True,
            reason=StopReason.CUSTOM,
            severity=StopSeverity.WARNING,
            confidence=0.8,
            details="Custom stop condition met",
            metrics={"custom_metric": value}
        )
    return None

stop_manager = StopCriteriaManager()
stop_manager.add_custom_criterion("custom_rule", custom_stop_criterion)
```

### Safety Constraints

```python
from safety_constraints import SafetyConstraint, ConstraintType, ConstraintSeverity

# Add custom constraint
custom_constraint = SafetyConstraint(
    name="Custom Risk Limit",
    constraint_type=ConstraintType.CUSTOM,
    severity=ConstraintSeverity.WARNING,
    parameters={"max_risk_score": 0.7}
)

safety_manager.add_constraint(custom_constraint)
```

## Reports and Visualizations

The system generates multiple types of reports:

### Report Types
- **Summary**: High-level overview with key metrics
- **Detailed**: Comprehensive analysis with all components
- **Executive**: Strategic insights for leadership
- **Technical**: System performance and algorithm analysis
- **Compliance**: Audit trail and constraint compliance

### Visualizations
- Budget allocation pie charts
- Performance trend lines
- Budget utilization bars
- Risk assessment heatmaps
- ROI scatter plots
- Exploration vs exploitation balance

## Monitoring and Alerts

The system provides comprehensive monitoring:

```python
# Get system status
status = system.get_system_status()
print(f"Active alerts: {status['active_alerts_count']}")
print(f"Critical alerts: {status['critical_alerts']}")

# Get recent signals
signals = system.monitor.get_recent_signals(limit=10)
for signal in signals:
    print(f"{signal.signal_type.value}: {signal.message}")

# Acknowledge alerts
for alert in system.monitor.get_active_alerts():
    system.monitor.acknowledge_alert(alert.alert_id)
```

## Deterministic Behavior

The system guarantees deterministic behavior:

```python
# Same input always produces same output
result1 = system.run_allocation_cycle()
result2 = system.run_allocation_cycle()

# Results will be identical when inputs are the same
assert result1['allocation_decisions'] == result2['allocation_decisions']
```

## Export and Import

```python
# Export system state
state_path = system.export_system_state("my_system_state.json")

# Load system state
new_system = ResearchBudgetAllocationSystem(total_budget=1000000)
new_system.load_system_state("my_system_state.json")
```

## File Structure

```
research_budget_allocator/
├── main_system.py                 # Main integration and entry point
├── research_budget_allocator.py   # Core allocation logic
├── exploration_exploitation_manager.py  # Exploration strategies
├── stop_criteria_manager.py       # Automatic stopping criteria
├── budget_reallocator.py          # Dynamic reallocation
├── feedback_monitor.py            # Monitoring and alerting
├── safety_constraints.py          # Safety and determinism
├── explainable_reporting.py       # Reports and visualizations
├── README.md                      # This file
└── examples/                      # Usage examples
    ├── basic_usage.py
    ├── advanced_configuration.py
    └── custom_strategies.py
```

## Requirements

- Python 3.8+
- numpy
- pandas
- scipy
- matplotlib
- seaborn

## Logging

The system provides comprehensive logging:

```python
import logging

# Configure logging level
logging.getLogger('research_budget_allocator').setLevel(logging.DEBUG)

# Logs are written to both file and console
# File: research_allocator.log
# Console: Standard output
```

## Performance Considerations

- System is designed for research budgets up to $100M+
- Can handle 1000+ research assets
- Allocation cycles complete in <1 second for typical workloads
- Memory usage scales linearly with number of assets

## Extensibility

The system is designed to be extensible:

1. **New Exploration Strategies**: Implement `ExplorationStrategyBase`
2. **Custom Stop Criteria**: Add custom validator functions
3. **Additional Constraints**: Implement `ConstraintValidator`
4. **Custom Reports**: Extend `ExplainableReportingSystem`
5. **New Metrics**: Add to `FeedbackMonitor`

## Best Practices

1. **Start with default configuration** and tune based on results
2. **Monitor alerts regularly** to catch issues early
3. **Review explanations** to understand decision logic
4. **Export system state** periodically for backup
5. **Use deterministic mode** for reproducible results
6. **Validate constraints** match your organizational requirements

## Troubleshooting

### Common Issues

1. **Low allocation amounts**: Check safety constraints and budget limits
2. **Too many stop decisions**: Review stop criteria thresholds
3. **High exploration rate**: Adjust exploration strategy parameters
4. **Memory usage**: Reduce monitoring window size
5. **Slow performance**: Enable deterministic caching

### Debug Mode

```python
# Enable debug logging
import logging
logging.basicConfig(level=logging.DEBUG)

# Run with detailed output
results = system.run_allocation_cycle()
```

## Contributing

The system is modular and designed for contributions:

1. Add new exploration strategies in `exploration_exploitation_manager.py`
2. Implement custom constraints in `safety_constraints.py`
3. Extend reporting capabilities in `explainable_reporting.py`
4. Add new visualization types
5. Improve performance and scalability

## License

This research budget allocation system is provided as-is for research and educational purposes.

## Support

For questions and support:
1. Review the code documentation
2. Check the examples directory
3. Examine the log files for detailed error information
4. Review the generated reports for system insights

---

**Note**: This system is designed for research budget allocation and should be adapted to your specific organizational requirements and constraints.
