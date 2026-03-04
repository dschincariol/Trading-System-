"""
Execution Capacity Manager

Enforces execution capacity limits and manages order flow constraints.
"""

import time
from typing import Dict, List, Optional, Tuple
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass
class CapacityLimits:
    max_orders_per_second: float = 10.0
    max_orders_per_minute: int = 100
    max_notional_per_minute: float = 100000.0
    max_concurrent_positions: int = 20
    min_order_interval_s: float = 0.1


class ExecutionCapacityManager:
    def __init__(self, limits: Optional[CapacityLimits] = None):
        self.limits = limits or CapacityLimits()
        self.order_times = deque(maxlen=1000)
        self.notional_tracker = deque(maxlen=1000)
        self.active_positions = set()
        
    def check_capacity(self, order_size: float, symbol: str) -> Tuple[bool, str]:
        """Check if order respects capacity limits"""
        now = time.time()
        
        # Rate limiting
        recent_orders = [t for t in self.order_times if now - t < 1.0]
        if len(recent_orders) >= self.limits.max_orders_per_second:
            return False, "Rate limit exceeded (per second)"
            
        recent_minute = [t for t in self.order_times if now - t < 60.0]
        if len(recent_minute) >= self.limits.max_orders_per_minute:
            return False, "Rate limit exceeded (per minute)"
            
        # Notional limits
        recent_notional = sum(n for t, n in self.notional_tracker if now - t < 60.0)
        if recent_notional + order_size > self.limits.max_notional_per_minute:
            return False, "Notional limit exceeded"
            
        # Position limits
        if symbol not in self.active_positions and len(self.active_positions) >= self.limits.max_concurrent_positions:
            return False, "Max concurrent positions exceeded"
            
        return True, "OK"
        
    def record_order(self, order_size: float, symbol: str):
        """Record order execution"""
        now = time.time()
        self.order_times.append(now)
        self.notional_tracker.append((now, order_size))
        self.active_positions.add(symbol)
        
    def close_position(self, symbol: str):
        """Remove position from active set"""
        self.active_positions.discard(symbol)
