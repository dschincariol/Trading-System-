#!/usr/bin/env python3
"""
Start Monitoring System

Entry point to start the complete monitoring and reliability system.
Integrates with existing dashboard server and provides startup coordination.
"""

import os
import sys
import time
import signal
import threading
from pathlib import Path

# Add project root to path
BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

try:
    from monitoring_orchestrator import monitoring_orchestrator
    from operator_dashboard import register_dashboard_routes
except ImportError as e:
    print(f"Import error: {e}")
    print("Make sure all monitoring modules are in the ops/ directory")
    sys.exit(1)

# Global flag for graceful shutdown
shutdown_requested = False

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    global shutdown_requested
    print(f"\nReceived signal {signum}, initiating graceful shutdown...")
    shutdown_requested = True
    monitoring_orchestrator.stop()
    sys.exit(0)

def start_dashboard_server():
    """Start the dashboard server with monitoring APIs"""
    try:
        from dashboard_server import app
        
        # Register monitoring dashboard routes
        register_dashboard_routes(app)
        
        print("Starting dashboard server with monitoring APIs...")
        print("Dashboard available at: http://localhost:8000/dashboard")
        print("API endpoints available at: http://localhost:8000/api/dashboard/*")
        
        app.run(host='0.0.0.0', port=8000, debug=False, threaded=True)
        
    except Exception as e:
        print(f"Error starting dashboard server: {e}")
        return False
    
    return True

def initialize_monitoring():
    """Initialize monitoring system components"""
    print("Initializing monitoring system...")
    
    # Initialize database tables if needed
    try:
        from engine.storage import init_db
        init_db()
        print("Database initialized")
    except Exception as e:
        print(f"Database initialization warning: {e}")
    
    # Start monitoring orchestrator
    monitoring_orchestrator.start()
    print("Monitoring orchestrator started")
    
    # Test basic functionality
    try:
        from monitoring_metrics import metrics_collector
        metrics_collector.record_system_health("monitoring_startup", 1.0)
        print("Metrics collector test passed")
    except Exception as e:
        print(f"Metrics collector test failed: {e}")
    
    print("Monitoring system initialization complete")

def main():
    """Main entry point"""
    print("=" * 60)
    print("Live Trading System - Monitoring & Reliability")
    print("=" * 60)
    
    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Initialize monitoring
    initialize_monitoring()
    
    # Start dashboard server in main thread
    try:
        start_dashboard_server()
    except KeyboardInterrupt:
        print("\nShutdown requested by user")
    except Exception as e:
        print(f"Fatal error: {e}")
    finally:
        # Ensure monitoring is stopped
        monitoring_orchestrator.stop()
        print("Monitoring system stopped")

if __name__ == "__main__":
    main()
