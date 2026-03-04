# Live Trading System - Monitoring & Reliability

## Overview

Comprehensive monitoring, alerting, and auto-remediation system for 24/7 live trading operations. Designed with fail-safe principles to protect live money while minimizing human intervention.

## System Architecture

### Core Components

1. **SLO Definitions** (`slo_definitions.py`)
   - Service Level Objectives for data freshness, model health, execution quality, and job reliability
   - Conservative thresholds with fail-safe defaults
   - Automated SLO evaluation and status tracking

2. **Metrics Collection** (`monitoring_metrics.py`)
   - Time-series metrics storage and aggregation
   - Real-time data collection from all system components
   - Efficient batch processing and windowed aggregations

3. **Alert Management** (`alert_manager.py`)
   - Intelligent alert generation with rate limiting
   - Multi-severity alerting (info, warning, critical, emergency)
   - Alert lifecycle management (acknowledge, resolve)

4. **Auto-Remediation** (`auto_remediation.py`)
   - Automated recovery playbooks for common failures
   - Service restarts, component quarantine, trading disable/enable
   - Safe execution with rollback capabilities

5. **Kill Switch Integration** (`kill_switch_integration.py`)
   - Automatic trading halt based on system conditions
   - Multi-level triggers (SLO violations, multiple alerts, data failures)
   - Manual override capabilities

6. **Escalation Rules** (`escalation_rules.py`)
   - Tiered notification system
   - Contact rotation and on-call schedules
   - Multi-channel notifications (email, Slack, SMS, pager)

7. **Operator Dashboard** (`operator_dashboard.py`)
   - Real-time system health visualization
   - Alert management and kill switch controls
   - Metrics charts and SLO compliance views

8. **Monitoring Orchestrator** (`monitoring_orchestrator.py`)
   - Central coordination of all monitoring components
   - Continuous monitoring cycles (30-second intervals)
   - Component health tracking and error recovery

## SLO Definitions

### Data Freshness
- **Price Data**: 99.5% target, max 2min warning, 5min critical
- **Market Events**: 99.0% target, max 10min warning, 30min critical  
- **Predictions**: 98.0% target, max 5min warning, 15min critical

### Model Health
- **Prediction Accuracy**: 85% target, 75% warning, 65% critical
- **Confidence Calibration**: 90% target, 80% warning, 70% critical
- **Model Drift**: 95% target, 0.15 KL warning, 0.25 critical
- **Prediction Latency**: 99% target, 5s warning, 15s critical

### Execution Quality
- **Fill Ratio**: 95% target, 85% warning, 75% critical
- **Execution Slippage**: 90% target, 5bp warning, 15bp critical
- **Order Latency**: 95% target, 2s warning, 5s critical
- **Position Reconciliation**: 99.9% target, 0.1% warning, 1% critical

### Job Reliability
- **Critical Job Uptime**: 99.9% target, 95% warning, 90% critical
- **Job Success Rate**: 99% target, 90% warning, 80% critical
- **Heartbeat Freshness**: 99.5% target, 5min warning, 10min critical

## Auto-Remediation Playbooks

### Stale Data Recovery
1. Restart data collector service
2. Clear price data cache
3. Notify operator

### Model Drift Recovery  
1. Rollback model to stable version
2. Disable trading for affected symbols (30min)
3. Notify operator

### Execution Failure Recovery
1. Flush pending orders
2. Quarantine execution engine (15min)
3. Restart execution service
4. Notify operator

### Job Failure Recovery
1. Restart job scheduler
2. Notify operator

## Kill Switch Triggers

### Automatic Triggers
- **Critical SLO Violations**: 2+ critical metrics for 2 consecutive checks
- **Multiple Critical Alerts**: 5+ critical alerts in 10 minutes
- **Data Freshness Failure**: Price/prediction data > 5 minutes stale
- **Execution Quality Failure**: Fill ratio < 50% or slippage > 25bp
- **Model Health Failure**: Accuracy < 50% or drift > 0.3

### Manual Triggers
- Operator dashboard emergency stop
- API-based manual trigger
- Environment variable override

## Escalation Policy

### Tier 1 (Immediate)
- **Contacts**: Primary Operations
- **Channels**: Email, Slack
- **Triggers**: Any critical alert

### Tier 2 (5 minutes)
- **Contacts**: Senior Operations  
- **Channels**: Email, Slack
- **Triggers**: Unresolved Tier 1, trading disabled

### Tier 3 (15 minutes)
- **Contacts**: Trading Manager
- **Channels**: Email, SMS
- **Triggers**: System failure, prolonged issues

### Emergency (30 minutes)
- **Contacts**: Emergency Response Team
- **Channels**: Pager, SMS
- **Triggers**: Critical system failure

## Dashboard Features

### System Health Overview
- Trading status (enabled/disabled)
- Active alerts count
- SLO violations count
- Kill switch status

### Real-time Metrics
- Data freshness indicators
- Model performance charts
- Execution quality metrics
- Job reliability status

### Alert Management
- Active alerts list with severity
- Alert acknowledgment and resolution
- Historical alert trends

### Controls
- Manual kill switch trigger
- Component quarantine management
- Remediation trigger controls

## Installation & Setup

### Prerequisites
```bash
pip install flask psutil
```

### Database Initialization
```python
from engine.storage import init_db
init_db()
```

### Start Monitoring System
```bash
cd ops/
python start_monitoring.py
```

### Access Dashboard
- **Dashboard**: http://localhost:8000/dashboard
- **API**: http://localhost:8000/api/dashboard/*

## Configuration

### Environment Variables
```bash
# Kill Switch Settings
KILL_SWITCH_REQUIRE_FRESH_DATA=1
KILL_SWITCH_MAX_PRICE_STALE_S=300
KILL_SWITCH_MAX_PRED_STALE_S=900
KILL_SWITCH_MAX_JOB_STALE_S=600

# Alert Settings
EQ_CRIT_EMAIL_TO=ops@tradingfirm.com
EQ_CRIT_SMTP_HOST=smtp.company.com
EQ_CRIT_WEBHOOK_URL=https://hooks.slack.com/...
```

### Monitoring Intervals
- **Orchestrator Cycle**: 30 seconds
- **Metrics Collection**: Continuous
- **SLO Evaluation**: Every cycle
- **Alert Rate Limit**: 10 alerts per 5 minutes

## Fail-Safe Principles

1. **Conservative Thresholds**: All SLOs use conservative targets
2. **Auto-Expiration**: Kill switches auto-expire after defined periods
3. **Rate Limiting**: Alert spam prevention
4. **Manual Override**: Human operators can always override
5. **Rollback Capability**: Auto-remediation actions can be undone
6. **Redundant Checks**: Multiple validation points before actions

## Monitoring Metrics

### System Metrics
- CPU, memory, disk usage
- Network connectivity
- Database performance

### Trading Metrics  
- Order flow rates
- Position changes
- PnL volatility
- Risk metrics

### Data Pipeline Metrics
- Data freshness
- Processing latency
- Error rates
- Queue depths

## Troubleshooting

### Common Issues

1. **Monitoring Not Starting**
   - Check database connectivity
   - Verify environment variables
   - Review log files

2. **Alerts Not Triggering**
   - Verify metrics collection
   - Check SLO thresholds
   - Review alert manager logs

3. **Auto-Remediation Failing**
   - Check service permissions
   - Verify systemd/supervisor setup
   - Review remediation logs

4. **Dashboard Not Loading**
   - Check Flask server status
   - Verify API endpoints
   - Review browser console

### Log Locations
- **Monitoring Logs**: `logs/monitoring.log`
- **Alert Logs**: Database `alerts` table
- **Remediation Logs**: Database `remediation_log` table

## API Reference

### Health Endpoints
- `GET /api/dashboard/health` - System health status
- `GET /api/dashboard/slos` - SLO compliance status

### Alert Endpoints  
- `GET /api/dashboard/alerts` - Active alerts
- `POST /api/dashboard/alerts/{id}/acknowledge` - Acknowledge alert

### Kill Switch Endpoints
- `GET /api/dashboard/kill-switch` - Kill switch status
- `POST /api/dashboard/kill-switch` - Manual trigger

### Metrics Endpoints
- `GET /api/dashboard/metrics` - Raw metrics data
- `GET /api/dashboard/metrics/aggregated` - Aggregated metrics

## Security Considerations

1. **Access Control**: Dashboard should be protected by authentication
2. **API Security**: Rate limiting and input validation
3. **Data Privacy**: Sensitive trading data protection
4. **Audit Trail**: All actions logged and traceable
5. **Network Security**: TLS encryption for communications

## Performance Impact

- **CPU Overhead**: < 2% additional CPU usage
- **Memory Overhead**: ~100MB for metrics storage
- **Network Impact**: Minimal internal traffic only
- **Storage Impact**: ~1GB/day for metrics data

## Maintenance

### Daily Tasks
- Review active alerts
- Check system health dashboard
- Verify auto-remediation success

### Weekly Tasks  
- Review SLO compliance trends
- Update contact information
- Check escalation paths

### Monthly Tasks
- Review and tune SLO thresholds
- Update remediation playbooks
- Audit kill switch configurations
