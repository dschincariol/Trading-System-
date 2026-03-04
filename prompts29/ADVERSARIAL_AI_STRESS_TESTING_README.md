# ADVERSARIAL_AI_STRESS_TESTING_README.md
# Adversarial AI Stress Testing System

## Overview

This system provides comprehensive adversarial stress testing for financial models, designed to test robustness under extreme but plausible market conditions. The system operates entirely offline with no connection to live trading execution.

## System Architecture

### Core Components

1. **Adversarial Scenario Generator** (`adversarial_scenario_generator.py`)
   - Generates synthetic market scenarios (crashes, regime shifts, liquidity droughts, news shocks)
   - 7 scenario types with realistic parameter ranges
   - Configurable severity levels and durations

2. **Stress Test Integration** (`stress_test_integration.py`)
   - Integrates with existing backtesting pipeline
   - Pre-promotion validation gates
   - Continuous monitoring capabilities

3. **Model Fragility Analyzer** (`model_fragility_analyzer.py`)
   - Multi-dimensional fragility scoring
   - Failure mode classification and clustering
   - Early warning indicators
   - Remediation recommendations

4. **Risk Reporting System** (`risk_reporting_system.py`)
   - Executive summary reports
   - Technical analysis reports
   - Regulatory compliance reports
   - Structured risk metrics

5. **CI Stress Testing** (`ci_stress_testing.py`)
   - Automated pass/fail thresholds
   - Multi-stage validation pipeline
   - Auto-rollback capabilities
   - Notification and alerting

## Scenario Types

### Market Scenarios
- **Market Crash**: Rapid price declines (-5z to -2z moves)
- **Regime Shift**: Volatility/spread changes (1.5x-4x volatility)
- **Liquidity Drought**: Order book thinning (5%-30% liquidity)
- **News Shock**: Sentiment-driven volatility (2.5x-10x volatility)
- **Flash Crash**: Extreme rapid moves (-8z to -3z in 1-10 minutes)
- **Volatility Spike**: Sudden volatility increases (3x-12x)
- **Spread Widening**: Execution cost increases (25-120 bps)

## Installation & Setup

### Prerequisites
- Python 3.8+
- Existing backtesting infrastructure
- SQLite database access

### Installation
```bash
# Copy files to research directory
cp adversarial_scenario_generator.py engine/research/
cp stress_test_integration.py engine/research/
cp model_fragility_analyzer.py engine/research/
cp risk_reporting_system.py engine/research/
cp ci_stress_testing.py engine/research/
```

### Database Setup
The system automatically creates required tables on first run:
- `adversarial_scenarios`
- `stress_test_summary`
- `model_fragility_profiles`
- `risk_reports`
- `ci_pipeline_runs`

## Usage

### Basic Stress Testing

```python
from engine.research.adversarial_scenario_generator import AdversarialScenarioGenerator

# Create generator
generator = AdversarialScenarioGenerator()

# Generate scenarios
scenarios = generator.generate_scenario_suite(
    scenario_types=[ScenarioType.MARKET_CRASH, ScenarioType.LIQUIDITY_DROUGHT],
    scenarios_per_type=3,
    severity_levels=[0.6, 0.8, 0.95]
)

# Run stress test suite
results = generator.run_stress_test_suite(scenarios, pass_threshold=0.7)

# Generate report
report = generator.generate_risk_report(results['test_batch_id'])
print(report)
```

### CI Pipeline Integration

```python
from engine.research.ci_stress_testing import CIStressTesting

# Run CI pipeline
ci_system = CIStressTesting()
result = ci_system.run_ci_pipeline(
    model_name="embed_regressor",
    model_kind="neural_net",
    gates=['pre_promotion_stress_test', 'fragility_analysis']
)

if result.overall_status == GateStatus.PASSED:
    print("✅ Model passed stress tests - can be promoted")
else:
    print("❌ Model failed stress tests - promotion blocked")
```

### Model Fragility Analysis

```python
from engine.research.model_fragility_analyzer import ModelFragilityAnalyzer

analyzer = ModelFragilityAnalyzer()
profile = analyzer.analyze_model_fragility(
    model_name="embed_regressor",
    model_kind="neural_net",
    model_ts_ms=latest_model_timestamp
)

print(f"Overall fragility: {profile.overall_fragility:.3f}")
print(f"Top failure modes: {[m.value for m in profile.failure_modes[:3]]}")
```

### Risk Reporting

```python
from engine.research.risk_reporting_system import RiskReportingSystem

reporter = RiskReportingSystem()

# Executive summary
exec_report = reporter.generate_executive_summary_report(
    model_name="embed_regressor",
    period_days=30
)

# Technical analysis
tech_report = reporter.generate_technical_analysis_report(
    model_name="embed_regressor",
    period_days=30
)

# Output reports
print(reporter.format_report_for_output(exec_report))
```

## CLI Usage

### Stress Testing CLI
```bash
# Run complete stress test suite
python engine/research/adversarial_scenario_generator.py --scenario-type market_crash --severity 0.8

# Generate report for existing test
python engine/research/adversarial_scenario_generator.py --report-only stress_test_1234567890
```

### CI Pipeline CLI
```bash
# Run CI pipeline
python engine/research/ci_stress_testing.py --model-name embed_regressor --model-kind neural_net

# View pipeline history
python engine/research/ci_stress_testing.py --model-name embed_regressor --history

# View gate configurations
python engine/research/ci_stress_testing.py --config
```

### Fragility Analysis CLI
```bash
# Analyze model fragility
python engine/research/model_fragility_analyzer.py --model-name embed_regressor --report

# View early warnings
python engine/research/model_fragility_analyzer.py --model-name embed_regressor --warnings

# Cluster failure patterns
python engine/research/model_fragility_analyzer.py --model-name embed_regressor --cluster
```

### Risk Reporting CLI
```bash
# Generate executive summary
python engine/research/risk_reporting_system.py --model-name embed_regressor --report-type executive_summary

# Generate technical analysis
python engine/research/risk_reporting_system.py --model-name embed_regressor --report-type technical_analysis --output report.md

# View report history
python engine/research/risk_reporting_system.py --model-name embed_regressor --history
```

## Configuration

### Stress Test Thresholds
```python
# Update CI gate thresholds
ci_system = CIStressTesting()
config = ci_system.default_thresholds['pre_promotion_stress_test']
config.pass_threshold = 0.85  # Require 85% pass rate
config.required_scenarios = 20  # Test 20 scenarios
ci_system.update_gate_config('pre_promotion_stress_test', config)
```

### Scenario Parameters
```python
# Customize scenario generation
generator = AdversarialScenarioGenerator()
scenario = generator.generate_scenario(
    scenario_type=ScenarioType.MARKET_CRASH,
    severity=0.9,  # High severity
    affected_symbols=['AAPL', 'MSFT', 'SPY'],
    start_ts_ms=specific_timestamp
)
```

### Integration Points

#### Training Pipeline Integration
```python
# In pipeline_train_and_eval.py
from engine.research.stress_test_integration import stress_test_promotion_guard

# Before model promotion
stress_result = stress_test_promotion_guard(
    model_name=MODEL_NAME,
    model_kind=kind,
    model_ts_ms=int(snap)
)

if stress_result['decision'] == 'block':
    print("❌ Model promotion blocked by stress tests")
    return 1
```

#### Continuous Monitoring
```python
# Scheduled stress testing (e.g., cron job)
from engine.research.stress_test_integration import run_scheduled_stress_tests

results = run_scheduled_stress_tests()
for model, result in results.items():
    if result['decision'] == 'alert':
        send_alert(model, result)
```

## Risk Metrics & Scoring

### Fragility Dimensions
- **Performance**: Return and Sharpe degradation (30% weight)
- **Risk**: Drawdown and volatility issues (30% weight)
- **Execution**: Cost and slippage problems (20% weight)
- **Prediction**: Accuracy and reliability (15% weight)
- **Stability**: Consistency across regimes (5% weight)

### Failure Modes
- Return degradation
- Excessive drawdown
- Volatility explosion
- Execution cost spike
- Prediction accuracy loss
- Correlation breakdown
- Liquidity crisis
- Regime misalignment

### CI Gate Thresholds
- **Pre-promotion**: 80% pass rate, 15+ scenarios
- **Fragility analysis**: Max 0.3 fragility score
- **Execution cost**: Max 2x cost increase
- **Prediction accuracy**: Max 0.1 RMSE increase
- **Continuous monitoring**: 70% pass rate

## Reports & Output

### Executive Summary Report
- Overall risk assessment
- Key performance metrics
- Critical findings
- Immediate actions required
- Resource requirements

### Technical Analysis Report
- Methodology overview
- Scenario-by-scenario analysis
- Fragility dimension breakdown
- Failure mode analysis
- Remediation recommendations

### Regulatory Compliance Report
- Governance overview
- Risk framework compliance
- Stress test coverage
- Model validation status
- Audit trail

## Monitoring & Alerting

### Early Warning Indicators
- High vulnerability to specific scenarios
- Frequent failure modes
- Slow recovery from stress events
- High execution cost sensitivity

### Notification Levels
- **Info**: Successful pipeline completion
- **Warning**: Minor issues or degraded performance
- **Error**: Failed gates or blocked promotions
- **Critical**: System errors or auto-rollback events

### Auto-Rollback Conditions
- Critical gate failures in production
- Multiple gate failures
- High fragility scores (>0.8)
- Execution cost explosions (>5x)

## Best Practices

### Stress Testing Frequency
- **Pre-promotion**: Always required
- **Continuous**: Daily or weekly for production models
- **Manual**: Ad-hoc for model changes or market events

### Scenario Coverage
- Test all major scenario types regularly
- Include multiple severity levels
- Cover different time horizons
- Test cross-regime behavior

### Threshold Management
- Set conservative thresholds for production
- Review thresholds quarterly
- Adjust based on model performance
- Document threshold changes

### Report Distribution
- Executive summaries: Monthly to senior management
- Technical analysis: Weekly to model teams
- Regulatory reports: Quarterly to compliance
- Incident reports: As needed for failures

## Troubleshooting

### Common Issues

#### Stress Test Failures
- Check scenario parameters are realistic
- Verify baseline model performance
- Review data quality and availability
- Ensure sufficient scenario coverage

#### High Fragility Scores
- Analyze failure mode patterns
- Review feature engineering
- Check regime compatibility
- Consider ensemble methods

#### CI Pipeline Errors
- Verify database connectivity
- Check model registry entries
- Review gate configurations
- Ensure sufficient resources

#### Report Generation Issues
- Check stress test completion
- Verify data availability
- Review report templates
- Check output permissions

### Debug Mode
```python
# Enable debug logging
import logging
logging.basicConfig(level=logging.DEBUG)

# Run with detailed output
generator = AdversarialScenarioGenerator()
generator.logger.setLevel(logging.DEBUG)
```

## Performance Considerations

### Execution Time
- Stress test suite: 5-30 minutes depending on scenarios
- Fragility analysis: 2-10 minutes
- Report generation: 1-5 minutes
- CI pipeline: 10-45 minutes total

### Resource Requirements
- CPU: Moderate (scenario generation)
- Memory: Low to moderate (result processing)
- Storage: Low (JSON results and reports)
- Network: Minimal (offline operation)

### Scaling
- Parallel scenario execution possible
- Database indexing for query performance
- Report caching for repeated requests
- Batch processing for multiple models

## Security & Compliance

### Data Protection
- All operations offline
- No live trading connections
- Encrypted sensitive configurations
- Audit trail for all actions

### Regulatory Alignment
- SR 11-7 model risk compliance
- Stress testing requirements
- Model validation documentation
- Governance framework support

### Access Controls
- Role-based report access
- Pipeline execution permissions
- Configuration modification controls
- Audit log maintenance

## Future Enhancements

### Planned Features
- Real-time scenario generation
- Machine learning-based scenario optimization
- Advanced clustering algorithms
- Interactive dashboard integration
- Multi-model correlation analysis

### Integration Opportunities
- Market data feeds for realistic scenarios
- Execution system integration for cost analysis
- Risk management system integration
- Compliance reporting automation
- Model monitoring platforms

## Support & Maintenance

### Regular Maintenance
- Database cleanup and optimization
- Scenario parameter updates
- Threshold review and adjustment
- Report template updates

### Monitoring Requirements
- System health checks
- Pipeline execution monitoring
- Error rate tracking
- Performance metric monitoring

### Documentation Updates
- Scenario parameter changes
- Threshold modifications
- New feature additions
- Best practice updates

---

## Quick Start Guide

1. **Install the system** by copying files to the research directory
2. **Run a basic stress test**: `python engine/research/adversarial_scenario_generator.py --scenario-type market_crash`
3. **Generate your first report**: `python engine/research/risk_reporting_system.py --model-name your_model --report-type executive_summary`
4. **Set up CI integration**: Add stress test gates to your model promotion pipeline
5. **Configure monitoring**: Set up scheduled stress tests and notifications

For detailed implementation guidance, see the individual module documentation and CLI help commands.

---

*This adversarial stress testing system is designed to enhance model robustness and provide comprehensive risk assessment while maintaining strict offline operation and regulatory compliance.*
