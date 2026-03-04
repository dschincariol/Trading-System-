#!/usr/bin/env python3
"""
Minimal test server for the explainability feature
"""
import json
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import sqlite3
import os

class ExplainabilityHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        
        # CORS headers
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        
        if path == '/api/ui/decisions':
            self.handle_get_decisions()
        elif path == '/api/ui/decision':
            decision_id = query.get('decision_id', [None])[0]
            if decision_id:
                self.handle_get_decision(decision_id)
            else:
                self.send_error_response('decision_id required')
        elif path == '/api/ui/overview':
            self.handle_get_overview()
        elif path.startswith('/ui/'):
            self.serve_static_file(path[4:])
        else:
            self.send_error_response('Not found')
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    
    def handle_get_decisions(self):
        # Mock decisions data
        decisions = [
            {
                "decision_id": "test_001",
                "ts_ms": int(time.time() * 1000) - 300000,
                "action": "increase",
                "symbol": "AAPL",
                "size_delta": 0.05,
                "certainty": 0.85,
                "risk_impact": "medium",
                "why": "Strong positive momentum detected with high confidence"
            },
            {
                "decision_id": "test_002", 
                "ts_ms": int(time.time() * 1000) - 600000,
                "action": "reduce",
                "symbol": "GOOG",
                "size_delta": 0.03,
                "certainty": 0.72,
                "risk_impact": "low",
                "why": "Moderate negative sentiment detected"
            }
        ]
        
        response = {
            "ok": True,
            "decisions": decisions,
            "count": len(decisions)
        }
        
        self.wfile.write(json.dumps(response).encode())
    
    def handle_get_decision(self, decision_id):
        # Mock detailed decision with explainability data
        decision = {
            "decision_id": decision_id,
            "ts_ms": int(time.time() * 1000) - 300000,
            "action": "increase",
            "symbol": "AAPL",
            "size_delta": 0.05,
            "certainty": 0.85,
            "risk_impact": "medium",
            "why": "Strong positive momentum detected with high confidence",
            "inputs_summary": {
                "from_weight": 0.10,
                "to_weight": 0.15,
                "current_side": "long",
                "current_weight": 0.12
            },
            "model_versions": ["momentum_v2", "sentiment_v1"],
            "confidence": 0.85,
            "risk_gates_triggered": ["position_size_check"],
            "allocation_before_after": {
                "before": 0.10,
                "after": 0.15,
                "change": 0.05
            },
            "decision_logs": [
                {
                    "model_name": "momentum_v2",
                    "model_kind": "regression",
                    "model_ts_ms": int(time.time() * 1000) - 300000,
                    "predicted_z": 1.8,
                    "confidence": 0.85,
                    "features": {"price_momentum": 1.2, "volume_spike": 0.8},
                    "explain": {"primary_factor": "price_momentum"}
                }
            ],
            "source_alert_id": "alert_123",
            # New explainability fields
            "top_drivers": [
                {
                    "type": "market_stress",
                    "name": "Market Stress",
                    "value": 0.3,
                    "impact": "medium"
                },
                {
                    "type": "sentiment",
                    "name": "Market Sentiment",
                    "value": 0.8,
                    "impact": "high"
                },
                {
                    "type": "feature",
                    "name": "Price Momentum",
                    "value": 1.2,
                    "model": "momentum_v2",
                    "impact": "high"
                }
            ],
            "model_outputs": [
                {
                    "model_name": "momentum_v2",
                    "model_kind": "regression",
                    "predicted_z": 1.8,
                    "confidence": 0.85,
                    "timestamp": int(time.time() * 1000) - 300000,
                    "interpretation": "Strong positive signal"
                }
            ],
            "plain_english_explanation": "Decision to increase AAPL position was driven by market sentiment (score: 0.80) and 2 other factors. Risk checks triggered: position_size_check. Action taken with high confidence."
        }
        
        response = {
            "ok": True,
            "decision": decision
        }
        
        self.wfile.write(json.dumps(response).encode())
    
    def handle_get_overview(self):
        response = {
            "ok": True,
            "ts_ms": int(time.time() * 1000),
            "system_health": "healthy",
            "market_environment": {
                "description": "Normal volatility conditions",
                "confidence": "Medium"
            },
            "capital_at_risk_today": 125000,
            "strategy_counts": {"active": 3, "paused": 1, "testing": 2},
            "last_decision_timestamp_ms": int(time.time() * 1000) - 300000,
            "alert_severity_counts_24h": {"critical": 0, "warning": 2, "info": 5}
        }
        
        self.wfile.write(json.dumps(response).encode())
    
    def serve_static_file(self, path):
        if path == '' or path == '/':
            path = 'dashboard.html'
        
        file_path = os.path.join('ui', path)
        
        try:
            with open(file_path, 'rb') as f:
                content = f.read()
                
            # Determine content type
            if file_path.endswith('.html'):
                content_type = 'text/html'
            elif file_path.endswith('.js'):
                content_type = 'application/javascript'
            elif file_path.endswith('.css'):
                content_type = 'text/css'
            else:
                content_type = 'text/plain'
            
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.end_headers()
            self.wfile.write(content)
            
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'File not found')
    
    def send_error_response(self, message):
        response = {
            "ok": False,
            "error": message
        }
        self.wfile.write(json.dumps(response).encode())
    
    def log_message(self, format, *args):
        # Suppress default logging
        pass

def run_server():
    server_address = ('', 8000)
    httpd = HTTPServer(server_address, ExplainabilityHandler)
    print("🚀 Explainability Test Server running at http://localhost:8000/ui/dashboard.html")
    print("📊 Test the 'Explain This Decision' feature with the 🔍 buttons")
    httpd.serve_forever()

if __name__ == '__main__':
    run_server()
