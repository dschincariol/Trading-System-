#!/usr/bin/env python3
"""
Simple test server for timeline functionality
"""
import os
import sys
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Add current directory to path
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

from dashboard_server import api_get_timeline

class TimelineTestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        
        if parsed.path == '/api/ui/timeline':
            # Parse query parameters
            query_params = parse_qs(parsed.query)
            params = {}
            for key, values in query_params.items():
                if values:
                    params[key] = values[0]
            
            # Call timeline API
            result = api_get_timeline(params)
            
            # Send response
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            
            response_data = json.dumps(result, indent=2)
            self.wfile.write(response_data.encode('utf-8'))
            
        elif parsed.path == '/':
            # Serve simple test page
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            
            html_content = """
<!DOCTYPE html>
<html>
<head>
    <title>Timeline Test</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        .container { max-width: 800px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h1 { color: #333; }
        .timeline-entry { border: 1px solid #ddd; margin: 10px 0; padding: 15px; border-radius: 5px; background: #fafafa; }
        .timeline-type { display: inline-block; padding: 4px 8px; border-radius: 3px; color: white; font-size: 12px; font-weight: bold; margin-right: 10px; }
        .INGEST { background: #58a6ff; }
        .MODEL { background: #7ee787; }
        .RISK { background: #d29922; }
        .DECISION { background: #f85149; }
        .EXECUTION { background: #a371f7; }
        .timeline-time { color: #666; font-size: 12px; }
        .timeline-label { font-weight: bold; margin-bottom: 5px; }
        .timeline-description { color: #555; margin-top: 5px; }
        button { background: #0366d6; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; margin: 5px; }
        button:hover { background: #0256cc; }
        .filter-controls { margin-bottom: 20px; }
        select { padding: 8px; border-radius: 5px; border: 1px solid #ddd; }
        .loading { text-align: center; color: #666; padding: 20px; }
        .error { color: #d32f2f; background: #ffebee; padding: 10px; border-radius: 5px; margin: 10px 0; }
    </style>
</head>
<body>
    <div class="container">
        <h1>⏰ System Activity Timeline</h1>
        
        <div class="filter-controls">
            <button onclick="loadTimeline()">Refresh</button>
            <select id="typeFilter" onchange="filterTimeline()">
                <option value="">All Types</option>
                <option value="INGEST">Ingest</option>
                <option value="MODEL">Model</option>
                <option value="RISK">Risk</option>
                <option value="DECISION">Decision</option>
                <option value="EXECUTION">Execution</option>
            </select>
            <span id="entryCount">Loading...</span>
        </div>
        
        <div id="timelineContainer">
            <div class="loading">Loading timeline...</div>
        </div>
    </div>

    <script>
        let allEntries = [];
        let currentFilter = '';

        async function loadTimeline() {
            const container = document.getElementById('timelineContainer');
            const countSpan = document.getElementById('entryCount');
            
            container.innerHTML = '<div class="loading">Loading timeline...</div>';
            
            try {
                const response = await fetch('/api/ui/timeline?limit=50');
                const data = await response.json();
                
                if (data.ok) {
                    allEntries = data.entries;
                    countSpan.textContent = `${data.count} entries`;
                    renderTimeline();
                } else {
                    container.innerHTML = `<div class="error">Error: ${data.error}</div>`;
                    countSpan.textContent = 'Error';
                }
            } catch (error) {
                container.innerHTML = `<div class="error">Network error: ${error.message}</div>`;
                countSpan.textContent = 'Error';
            }
        }

        function filterTimeline() {
            currentFilter = document.getElementById('typeFilter').value;
            renderTimeline();
        }

        function renderTimeline() {
            const container = document.getElementById('timelineContainer');
            
            let entries = allEntries;
            if (currentFilter) {
                entries = allEntries.filter(entry => entry.type === currentFilter);
            }
            
            if (entries.length === 0) {
                container.innerHTML = '<div class="loading">No entries found</div>';
                return;
            }
            
            const html = entries.map(entry => {
                const time = new Date(entry.ts_ms).toLocaleString();
                return `
                    <div class="timeline-entry" onclick="handleEntryClick('${entry.type}', '${entry.reference_id}')">
                        <div>
                            <span class="timeline-type ${entry.type}">${entry.type}</span>
                            <span class="timeline-time">${time}</span>
                        </div>
                        <div class="timeline-label">${entry.label}</div>
                        <div class="timeline-description">${entry.description}</div>
                    </div>
                `;
            }).join('');
            
            container.innerHTML = html;
        }

        function handleEntryClick(type, referenceId) {
            alert(`Clicked ${type} entry with reference ID: ${referenceId}`);
        }

        // Load timeline on page load
        loadTimeline();
        
        // Auto-refresh every 30 seconds
        setInterval(loadTimeline, 30000);
    </script>
</body>
</html>
            """
            self.wfile.write(html_content.encode('utf-8'))
            
        else:
            # 404
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not Found')

    def log_message(self, format, *args):
        # Suppress log messages
        pass

def run_test_server():
    port = 8001
    server = HTTPServer(('127.0.0.1', port), TimelineTestHandler)
    print(f"Timeline test server running at http://127.0.0.1:{port}/")
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.shutdown()

if __name__ == '__main__':
    run_test_server()
