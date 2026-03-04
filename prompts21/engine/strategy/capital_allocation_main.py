"""
Capital Allocation Engine - Main Entry Point

Integrates all components for production deployment.
"""

import time
import json
from typing import List, Dict
from engine.storage import connect
from engine.strategy.capital_allocation_engine import CapitalAllocationEngine, run_capital_allocation
from engine.strategy.execution_integration import ExecutionIntegration
from engine.strategy.risk_constraints import get_default_constraints


def main():
    """Main capital allocation loop"""
    print("Starting Capital Allocation Engine...")
    
    # Initialize components
    engine = CapitalAllocationEngine()
    execution = ExecutionIntegration()
    
    # Main allocation loop
    while True:
        try:
            # Get current positions
            current_positions = get_current_positions()
            
            # Run allocation
            targets = run_capital_allocation()
            
            if targets:
                print(f"Generated {len(targets)} allocation targets")
                
                # Convert to orders
                orders = execution.convert_allocations_to_orders(targets, current_positions)
                
                if orders:
                    print(f"Submitting {len(orders)} orders")
                    success = execution.submit_orders(orders)
                    print(f"Order submission: {'SUCCESS' if success else 'FAILED'}")
            
            # Wait for next cycle
            time.sleep(60)  # 1-minute cycles
            
        except Exception as e:
            print(f"Error in allocation cycle: {e}")
            time.sleep(30)


def get_current_positions() -> Dict[str, float]:
    """Get current portfolio positions"""
    con = connect()
    try:
        query = "SELECT symbol, weight FROM positions WHERE weight != 0"
        cursor = con.execute(query)
        return dict(cursor.fetchall())
    finally:
        con.close()


if __name__ == "__main__":
    main()
