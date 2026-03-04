"""
Execution Layer Integration

Connects capital allocation engine with execution layer.
"""

from typing import List, Dict, Any
from engine.execution.broker_router import submit_orders
from engine.strategy.capital_allocation_engine import AllocationTarget


class ExecutionIntegration:
    def __init__(self):
        self.pending_orders = []
        
    def convert_allocations_to_orders(self, targets: List[AllocationTarget], current_positions: Dict[str, float]) -> List[Dict]:
        """Convert allocation targets to executable orders"""
        orders = []
        
        for target in targets:
            symbol = target.asset
            target_weight = target.weight
            current_weight = current_positions.get(symbol, 0.0)
            
            # Calculate order size
            weight_change = target_weight - current_weight
            if abs(weight_change) < 0.01:  # Skip small changes
                continue
                
            order = {
                'symbol': symbol,
                'side': 'buy' if weight_change > 0 else 'sell',
                'weight_change': weight_change,
                'strategy': target.strategy,
                'horizon': target.horizon,
                'confidence': target.confidence,
                'expected_return': target.expected_return
            }
            orders.append(order)
            
        return orders
        
    def submit_orders(self, orders: List[Dict]) -> bool:
        """Submit orders to execution layer"""
        try:
            # Convert to execution layer format
            exec_orders = []
            for order in orders:
                exec_order = {
                    'symbol': order['symbol'],
                    'side': order['side'],
                    'quantity': self._calculate_quantity(order),
                    'order_type': 'market',
                    'time_in_force': 'day',
                    'strategy': order['strategy']
                }
                exec_orders.append(exec_order)
                
            # Submit to broker router
            return submit_orders(exec_orders)
            
        except Exception as e:
            print(f"Order submission failed: {e}")
            return False
            
    def _calculate_quantity(self, order: Dict) -> int:
        """Calculate order quantity from weight change"""
        # This would use current portfolio value and price data
        # Simplified implementation
        return int(abs(order['weight_change']) * 100)
