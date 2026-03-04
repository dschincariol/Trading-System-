"""
Operator Dashboard API Endpoints

Provides REST API endpoints for operator dashboard including:
- System health overview
- Active alerts and SLO status
- Kill switch status and controls
- Auto-remediation status
- Metrics visualization data
"""

import json
import time
from typing import Dict, Any, List, Optional
from flask import Flask, jsonify, request

from slo_definitions import slo_evaluator, SLOStatus
from alert_manager import alert_manager, AlertSeverity
from monitoring_metrics import metrics_collector
from auto_remediation import auto_remediation, RemediationStatus
from kill_switch_integration import kill_switch_integration, KillSwitchTrigger

class OperatorDashboardAPI:
    """API endpoints for operator dashboard"""
    
    def __init__(self, app: Flask):
        self.app = app
        self._register_routes()
    
    def _register_routes(self):
        """Register all dashboard API routes"""
        
        @self.app.route('/api/dashboard/health', methods=['GET'])
        def get_system_health():
            """Get overall system health status"""
            try:
                health = kill_switch_integration.check_system_health()
                return jsonify({
                    "status": "success",
                    "data": health
                })
            except Exception as e:
                return jsonify({
                    "status": "error",
                    "message": str(e)
                }), 500
        
        @self.app.route('/api/dashboard/slos', methods=['GET'])
        def get_slo_status():
            """Get SLO compliance status"""
            try:
                slo_data = {}
                
                for category, slo_dict in slo_evaluator.get_all_slos().items():
                    slo_data[category] = {}
                    
                    for slo_name, slo_def in slo_dict.items():
                        latest_value = metrics_collector.get_latest_value(slo_def.name)
                        status = slo_evaluator.evaluate_metric(slo_def, latest_value) if latest_value is not None else SLOStatus.WARNING
                        
                        slo_data[category][slo_name] = {
                            "name": slo_def.name,
                            "description": slo_def.description,
                            "target": slo_def.target_percent,
                            "current": latest_value,
                            "status": status.value,
                            "unit": slo_def.unit,
                            "threshold_warning": slo_def.alert_threshold_warning,
                            "threshold_critical": slo_def.alert_threshold_critical
                        }
                
                return jsonify({
                    "status": "success",
                    "data": slo_data
                })
            except Exception as e:
                return jsonify({
                    "status": "error", 
                    "message": str(e)
                }), 500
        
        @self.app.route('/api/dashboard/alerts', methods=['GET'])
        def get_active_alerts():
            """Get active alerts with optional filtering"""
            try:
                severity_filter = request.args.get('severity')
                limit = int(request.args.get('limit', 50))
                
                if severity_filter:
                    try:
                        severity = AlertSeverity(severity_filter.lower())
                        alerts = alert_manager.get_active_alerts(severity)[:limit]
                    except ValueError:
                        return jsonify({
                            "status": "error",
                            "message": f"Invalid severity: {severity_filter}"
                        }), 400
                else:
                    alerts = alert_manager.get_active_alerts()[:limit]
                
                alert_data = []
                for alert in alerts:
                    alert_data.append({
                        "id": alert.id,
                        "metric_name": alert.metric_name,
                        "severity": alert.severity.value,
                        "status": alert.status.value,
                        "current_value": alert.current_value,
                        "threshold_value": alert.threshold_value,
                        "message": alert.message,
                        "tags": alert.tags,
                        "created_ms": alert.created_ms,
                        "acknowledged": alert.acknowledged
                    })
                
                return jsonify({
                    "status": "success",
                    "data": alert_data
                })
            except Exception as e:
                return jsonify({
                    "status": "error",
                    "message": str(e)
                }), 500
        
        @self.app.route('/api/dashboard/kill-switch', methods=['GET'])
        def get_kill_switch_status():
            """Get kill switch status"""
            try:
                from engine.execution.kill_switch import snapshot
                ks_snapshot = snapshot()
                
                return jsonify({
                    "status": "success",
                    "data": ks_snapshot
                })
            except Exception as e:
                return jsonify({
                    "status": "error",
                    "message": str(e)
                }), 500
        
        @self.app.route('/api/dashboard/kill-switch', methods=['POST'])
        def trigger_kill_switch():
            """Manual kill switch trigger"""
            try:
                data = request.get_json()
                if not data:
                    return jsonify({
                        "status": "error",
                        "message": "JSON data required"
                    }), 400
                
                scope = data.get('scope', 'global')
                reason = data.get('reason', 'Manual trigger')
                duration_minutes = data.get('duration_minutes')
                actor = data.get('actor', 'operator')
                
                trigger_id = kill_switch_integration.manual_trigger(
                    scope=scope,
                    reason=reason,
                    duration_minutes=duration_minutes,
                    actor=actor
                )
                
                return jsonify({
                    "status": "success",
                    "data": {
                        "trigger_id": trigger_id,
                        "scope": scope,
                        "reason": reason,
                        "duration_minutes": duration_minutes
                    }
                })
            except Exception as e:
                return jsonify({
                    "status": "error",
                    "message": str(e)
                }), 500
        
        @self.app.route('/api/dashboard/remediation', methods=['GET'])
        def get_remediation_status():
            """Get auto-remediation status"""
            try:
                from engine.storage import connect
                with connect() as con:
                    rows = con.execute("""
                        SELECT id, playbook_name, trigger_alert_id, status, 
                               started_ms, completed_ms, steps_executed, 
                               steps_total, error_message
                        FROM remediation_log 
                        ORDER BY started_ms DESC 
                        LIMIT 20
                    """).fetchall()
                
                remediation_data = []
                for row in rows:
                    remediation_data.append({
                        "id": row[0],
                        "playbook_name": row[1],
                        "trigger_alert_id": row[2],
                        "status": row[3],
                        "started_ms": row[4],
                        "completed_ms": row[5],
                        "steps_executed": row[6],
                        "steps_total": row[7],
                        "error_message": row[8]
                    })
                
                return jsonify({
                    "status": "success",
                    "data": remediation_data
                })
            except Exception as e:
                return jsonify({
                    "status": "error",
                    "message": str(e)
                }), 500
        
        @self.app.route('/api/dashboard/metrics', methods=['GET'])
        def get_metrics_data():
            """Get metrics data for visualization"""
            try:
                metric_name = request.args.get('metric')
                window_minutes = int(request.args.get('window', 60))
                
                if not metric_name:
                    return jsonify({
                        "status": "error",
                        "message": "metric parameter required"
                    }), 400
                
                metrics = metrics_collector.get_recent_metrics(metric_name, window_minutes)
                
                metrics_data = []
                for metric in metrics:
                    metrics_data.append({
                        "timestamp_ms": metric.timestamp_ms,
                        "value": metric.value,
                        "tags": metric.tags,
                        "source": metric.source
                    })
                
                return jsonify({
                    "status": "success",
                    "data": metrics_data
                })
            except Exception as e:
                return jsonify({
                    "status": "error",
                    "message": str(e)
                }), 500
        
        @self.app.route('/api/dashboard/metrics/aggregated', methods=['GET'])
        def get_aggregated_metrics():
            """Get aggregated metrics data"""
            try:
                metric_name = request.args.get('metric')
                window_minutes = int(request.args.get('window', 5))
                aggregation_type = request.args.get('agg', 'avg')
                
                if not metric_name:
                    return jsonify({
                        "status": "error",
                        "message": "metric parameter required"
                    }), 400
                
                aggregations = metrics_collector.aggregate_metrics(
                    metric_name, window_minutes, aggregation_type
                )
                
                agg_data = []
                for agg in aggregations:
                    agg_data.append({
                        "metric_name": agg.metric_name,
                        "window_minutes": agg.window_minutes,
                        "aggregation_type": agg.aggregation_type,
                        "value": agg.value,
                        "sample_count": agg.sample_count,
                        "window_start_ms": agg.window_start_ms,
                        "window_end_ms": agg.window_end_ms
                    })
                
                return jsonify({
                    "status": "success",
                    "data": agg_data
                })
            except Exception as e:
                return jsonify({
                    "status": "error",
                    "message": str(e)
                }), 500
        
        @self.app.route('/api/dashboard/quarantine', methods=['GET'])
        def get_quarantine_status():
            """Get component quarantine status"""
            try:
                from engine.storage import connect
                with connect() as con:
                    rows = con.execute("""
                        SELECT component_name, quarantined_ms, reason, 
                               auto_release_ms, released_ms
                        FROM component_quarantine 
                        WHERE released_ms IS NULL
                        ORDER BY quarantined_ms DESC
                    """).fetchall()
                
                quarantine_data = []
                for row in rows:
                    quarantine_data.append({
                        "component": row[0],
                        "quarantined_ms": row[1],
                        "reason": row[2],
                        "auto_release_ms": row[3],
                        "released_ms": row[4]
                    })
                
                return jsonify({
                    "status": "success",
                    "data": quarantine_data
                })
            except Exception as e:
                return jsonify({
                    "status": "error",
                    "message": str(e)
                }), 500

# Dashboard HTML template (simplified for integration)
DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Trading System Operations Dashboard</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        .header { background: #2c3e50; color: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }
        .card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .status-healthy { color: #27ae60; font-weight: bold; }
        .status-warning { color: #f39c12; font-weight: bold; }
        .status-critical { color: #e74c3c; font-weight: bold; }
        .metric-value { font-size: 24px; font-weight: bold; }
        .metric-label { color: #7f8c8d; margin-bottom: 5px; }
        .alert-item { padding: 10px; margin: 5px 0; border-left: 4px solid #3498db; background: #ecf0f1; }
        .alert-critical { border-left-color: #e74c3c; }
        .alert-warning { border-left-color: #f39c12; }
        .btn { padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; margin: 5px; }
        .btn-danger { background: #e74c3c; color: white; }
        .btn-success { background: #27ae60; color: white; }
        .refresh-btn { float: right; }
        .chart-container { height: 300px; margin: 20px 0; }
    </style>
</head>
<body>
    <div class="header">
        <h1>Trading System Operations Dashboard</h1>
        <button class="btn refresh-btn" onclick="refreshData()">Refresh</button>
    </div>
    
    <div class="grid">
        <!-- System Health Card -->
        <div class="card">
            <h3>System Health</h3>
            <div id="system-health">
                <div class="metric-label">Trading Status</div>
                <div class="metric-value" id="trading-status">Loading...</div>
                <div class="metric-label">Active Alerts</div>
                <div class="metric-value" id="active-alerts">-</div>
                <div class="metric-label">SLO Violations</div>
                <div class="metric-value" id="slo-violations">-</div>
            </div>
        </div>
        
        <!-- Kill Switch Status Card -->
        <div class="card">
            <h3>Kill Switch Status</h3>
            <div id="kill-switch-status">
                <div class="metric-label">Active Switches</div>
                <div class="metric-value" id="active-switches">-</div>
                <button class="btn btn-danger" onclick="triggerKillSwitch()">Emergency Stop</button>
            </div>
        </div>
        
        <!-- Recent Alerts Card -->
        <div class="card">
            <h3>Recent Alerts</h3>
            <div id="recent-alerts">
                Loading alerts...
            </div>
        </div>
        
        <!-- SLO Status Card -->
        <div class="card">
            <h3>SLO Compliance</h3>
            <div id="slo-status">
                Loading SLO data...
            </div>
        </div>
    </div>
    
    <!-- Metrics Charts -->
    <div class="card">
        <h3>System Metrics</h3>
        <div class="chart-container">
            <canvas id="metrics-chart"></canvas>
        </div>
    </div>
    
    <script>
        let metricsChart = null;
        
        async function fetchData(endpoint) {
            try {
                const response = await fetch(endpoint);
                const data = await response.json();
                return data;
            } catch (error) {
                console.error('Error fetching data:', error);
                return null;
            }
        }
        
        async function refreshData() {
            // Refresh system health
            const health = await fetchData('/api/dashboard/health');
            if (health && health.status === 'success') {
                const data = health.data;
                document.getElementById('trading-status').textContent = data.trading_allowed ? 'ENABLED' : 'DISABLED';
                document.getElementById('trading-status').className = data.trading_allowed ? 'status-healthy' : 'status-critical';
                document.getElementById('active-alerts').textContent = data.critical_alerts;
                document.getElementById('slo-violations').textContent = data.slo_violations;
            }
            
            // Refresh kill switch status
            const ks = await fetchData('/api/dashboard/kill-switch');
            if (ks && ks.status === 'success') {
                const activeSwitches = ks.data.state.filter(s => s.enabled === 1).length;
                document.getElementById('active-switches').textContent = activeSwitches;
            }
            
            // Refresh alerts
            const alerts = await fetchData('/api/dashboard/alerts');
            if (alerts && alerts.status === 'success') {
                const alertsHtml = alerts.data.slice(0, 5).map(alert => {
                    const severityClass = alert.severity === 'critical' ? 'alert-critical' : 'alert-warning';
                    return `
                        <div class="alert-item ${severityClass}">
                            <strong>${alert.metric_name}</strong> - ${alert.message}
                            <br><small>${new Date(alert.created_ms).toLocaleString()}</small>
                        </div>
                    `;
                }).join('');
                document.getElementById('recent-alerts').innerHTML = alertsHtml || '<div>No active alerts</div>';
            }
            
            // Refresh SLO status
            const slos = await fetchData('/api/dashboard/slos');
            if (slos && slos.status === 'success') {
                let sloHtml = '';
                for (const [category, sloDict] of Object.entries(slos.data)) {
                    sloHtml += `<h4>${category}</h4>`;
                    for (const [name, slo] of Object.entries(sloDict)) {
                        const statusClass = slo.status === 'healthy' ? 'status-healthy' : 
                                         slo.status === 'warning' ? 'status-warning' : 'status-critical';
                        sloHtml += `
                            <div style="margin: 10px 0;">
                                <div class="metric-label">${slo.description}</div>
                                <div class="${statusClass}">${slo.status.toUpperCase()}</div>
                                <small>Current: ${slo.current?.toFixed(2) || 'N/A'} | Target: ${slo.target}</small>
                            </div>
                        `;
                    }
                }
                document.getElementById('slo-status').innerHTML = sloHtml;
            }
        }
        
        async function triggerKillSwitch() {
            if (confirm('Are you sure you want to trigger emergency kill switch? This will stop all trading.')) {
                const response = await fetch('/api/dashboard/kill-switch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        scope: 'global',
                        reason: 'Manual emergency stop via dashboard',
                        actor: 'dashboard_operator'
                    })
                });
                
                if (response.ok) {
                    alert('Kill switch triggered successfully');
                    refreshData();
                } else {
                    alert('Failed to trigger kill switch');
                }
            }
        }
        
        // Initialize dashboard
        refreshData();
        setInterval(refreshData, 30000); // Refresh every 30 seconds
    </script>
</body>
</html>
"""

def register_dashboard_routes(app: Flask):
    """Register dashboard API routes with Flask app"""
    OperatorDashboardAPI(app)
    
    @app.route('/dashboard')
    def dashboard_page():
        """Serve dashboard HTML page"""
        return DASHBOARD_HTML
