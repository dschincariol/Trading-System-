# engine/strategy/rebalancing_manager.py
"""
Rebalancing Manager

Handles rebalancing cadence and execution integration for the capital allocation engine.
Implements stable rebalancing with overtrading protection.
"""

import os
import time
import json
import threading
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from collections import defaultdict

from engine.storage import connect
from engine.strategy.capital_allocation_engine import CapitalAllocationEngine, AllocationTarget
from engine.execution.broker_router import apply_new_portfolio_orders_router


@dataclass
class RebalancingConfig:
    # Rebalancing cadence
    base_interval_s: int = 300  # 5 minutes base
    volatility_multiplier: float = 1.5  # Scale interval in high vol
    min_interval_s: int = 60  # 1 minute minimum
    max_interval_s: int = 1800  # 30 minutes maximum
    
    # Overtrading protection
    turnover_threshold: float = 0.3  # 30% turnover triggers slowdown
    turnover_penalty_factor: float = 2.0  # Multiply interval by this
    min_position_age_s: int = 1800  # 30 minutes minimum hold
    
    # Execution integration
    dry_run: bool = False
    execution_delay_s: int = 5  # Delay between allocation and execution
    max_slippage_bps: float = 25.0  # Max acceptable slippage


class RebalancingManager:
    """Manages rebalancing cadence and execution integration."""
    
    def __init__(self, config: Optional[RebalancingConfig] = None):
        self.config = config or RebalancingConfig()
        self.allocation_engine = CapitalAllocationEngine()
        self.last_rebalance_ts = 0
        self.position_history = defaultdict(list)
        self.turnover_history = []
        self.is_running = False
        self._lock = threading.Lock()
        
    def calculate_next_rebalance_time(self) -> int:
        """Calculate when next rebalance should occur based on market conditions."""
        now = int(time.time())
        
        # Base interval
        interval = self.config.base_interval_s
        
        # Adjust for market volatility
        vol_multiplier = self._get_volatility_multiplier()
        interval = int(interval * vol_multiplier)
        
        # Adjust for recent turnover (overtrading protection)
        turnover_penalty = self._get_turnover_penalty()
        interval = int(interval * turnover_penalty)
        
        # Clamp to bounds
        interval = max(self.config.min_interval_s, min(self.config.max_interval_s, interval))
        
        return now + interval
    
    def should_rebalance(self) -> bool:
        """Check if rebalancing should occur now."""
        now = int(time.time())
        
        # Check minimum interval
        if now - self.last_rebalance_ts < self.config.min_interval_s:
            return False
        
        # Check if we're past the scheduled time
        next_rebalance_time = self.calculate_next_rebalance_time()
        return now >= next_rebalance_time
    
    def execute_rebalance(self, dry_run: Optional[bool] = None) -> Dict[str, Any]:
        """Execute a rebalance cycle."""
        if dry_run is None:
            dry_run = self.config.dry_run
            
        with self._lock:
            if not self.should_rebalance():
                return {"ok": False, "status": "not_time_to_rebalance"}
            
            try:
                # Get new allocation targets
                targets = self.allocation_engine.allocate_capital([])
                
                if not targets:
                    return {"ok": False, "status": "no_allocation_targets"}
                
                # Convert to execution orders
                orders = self._targets_to_orders(targets)
                
                if not orders:
                    return {"ok": False", "status": "no_orders_generated"}
                
                # Apply overtrading filters
                filtered_orders = self._apply_overtrading_filters(orders)
                
                if not filtered_orders:
                    return {"ok": False, "status": "orders_filtered_out"}
                
                # Execute orders
                execution_result = self._execute_orders(filtered_orders, dry_run)
                
                # Update tracking
                self._update_tracking(targets, execution_result)
                
                self.last_rebalance_ts = int(time.time())
                
                return {
                    "ok": True,
                    "status": "rebalance_completed",
                    "targets_count": len(targets),
                    "orders_count": len(filtered_orders),
                    "execution_result": execution_result,
                    "next_rebalance_ts": self.calculate_next_rebalance_time()
                }
                
            except Exception as e:
                return {
                    "ok": False,
                    "status": "rebalance_failed",
                    "error": str(e)
                }
    
    def _get_volatility_multiplier(self) -> float:
        """Get volatility-based interval multiplier."""
        try:
            con = connect()
            
            # Get recent VIX or market volatility
            rows = con.execute("""
                SELECT price FROM prices 
                WHERE symbol = 'VIX' 
                ORDER BY ts_ms DESC 
                LIMIT 1
            """).fetchall()
            
            if rows:
                vix = float(rows[0][0])
                # Higher VIX = longer intervals (more cautious)
                if vix > 30:
                    return self.config.volatility_multiplier
                elif vix > 20:
                    return 1.2
                else:
                    return 1.0
            
            return 1.0
            
        except Exception:
            return 1.0
        finally:
            if 'con' in locals():
                con.close()
    
    def _get_turnover_penalty(self) -> float:
        """Get turnover-based interval multiplier."""
        if not self.turnover_history:
            return 1.0
        
        # Calculate recent average turnover
        recent_turnover = self.turnover_history[-5:]  # Last 5 rebalances
        avg_turnover = sum(recent_turnover) / len(recent_turnover)
        
        if avg_turnover > self.config.turnover_threshold:
            return self.config.turnover_penalty_factor
        else:
            return 1.0
    
    def _targets_to_orders(self, targets: List[AllocationTarget]) -> List[Dict]:
        """Convert allocation targets to execution orders."""
        orders = []
        total_capital = self.allocation_engine.constraints.total_capital
        
        for target in targets:
            notional = target.weight * total_capital
            
            orders.append({
                "symbol": target.asset,
                "strategy": target.strategy,
                "horizon": target.horizon,
                "weight": target.weight,
                "notional": notional,
                "expected_return": target.expected_return,
                "confidence": target.confidence,
                "correlation_penalty": target.correlation_penalty,
                "execution_capacity_limit": target.execution_capacity_limit
            })
        
        return orders
    
    def _apply_overtrading_filters(self, orders: List[Dict]) -> List[Dict]:
        """Apply filters to prevent overtrading."""
        filtered_orders = []
        now = int(time.time())
        
        for order in orders:
            symbol = order["symbol"]
            
            # Check minimum position age
            last_position_time = self._get_last_position_time(symbol)
            if last_position_time and (now - last_position_time) < self.config.min_position_age_s:
                continue  # Skip to prevent overtrading
            
            # Check if allocation change is significant
            current_weight = self._get_current_weight(symbol)
            target_weight = order["weight"]
            
            if abs(target_weight - current_weight) < 0.05:  # 5% minimum change
                continue
            
            filtered_orders.append(order)
        
        return filtered_orders
    
    def _execute_orders(self, orders: List[Dict], dry_run: bool) -> Dict[str, Any]:
        """Execute orders through the broker router."""
        if dry_run:
            return {"ok": True, "status": "dry_run_success", "orders_count": len(orders)}
        
        # Convert to broker router format
        broker_orders = []
        for order in orders:
            broker_orders.append({
                "symbol": order["symbol"],
                "qty": order["notional"],  # Simplified - would need price conversion
                "order_type": "market",
                "time_in_force": "IOC",
                "strategy": order["strategy"],
                "allocation_metadata": {
                    "horizon": order["horizon"],
                    "weight": order["weight"],
                    "confidence": order["confidence"],
                    "correlation_penalty": order["correlation_penalty"]
                }
            })
        
        # Execute through broker router
        try:
            result = apply_new_portfolio_orders_router(
                dry_run=False,
                override_orders=broker_orders
            )
            
            return result
            
        except Exception as e:
            return {
                "ok": False,
                "status": "execution_failed",
                "error": str(e)
            }
    
    def _update_tracking(self, targets: List[AllocationTarget], execution_result: Dict[str, Any]):
        """Update internal tracking after rebalance."""
        now = int(time.time())
        
        # Update position history
        for target in targets:
            self.position_history[target.asset].append({
                "timestamp": now,
                "weight": target.weight,
                "strategy": target.strategy,
                "horizon": target.horizon
            })
            
            # Keep only recent history
            if len(self.position_history[target.asset]) > 100:
                self.position_history[target.asset] = self.position_history[target.asset][-100:]
        
        # Calculate and store turnover
        turnover = self._calculate_turnover(targets)
        self.turnover_history.append(turnover)
        
        # Keep only recent turnover history
        if len(self.turnover_history) > 20:
            self.turnover_history = self.turnover_history[-20:]
    
    def _get_last_position_time(self, symbol: str) -> Optional[int]:
        """Get timestamp of last position change for symbol."""
        history = self.position_history.get(symbol, [])
        if not history:
            return None
        return history[-1]["timestamp"]
    
    def _get_current_weight(self, symbol: str) -> float:
        """Get current weight for symbol."""
        history = self.position_history.get(symbol, [])
        if not history:
            return 0.0
        return history[-1]["weight"]
    
    def _calculate_turnover(self, targets: List[AllocationTarget]) -> float:
        """Calculate portfolio turnover for this rebalance."""
        total_change = 0.0
        
        for target in targets:
            current_weight = self._get_current_weight(target.asset)
            change = abs(target.weight - current_weight)
            total_change += change
        
        return total_change / 2.0  # Divide by 2 for double-counting
    
    def start_rebalancing_loop(self):
        """Start the automatic rebalancing loop."""
        self.is_running = True
        
        def rebalance_loop():
            while self.is_running:
                try:
                    if self.should_rebalance():
                        result = self.execute_rebalance()
                        print(f"Rebalance result: {json.dumps(result, indent=2)}")
                    
                    # Sleep until next check
                    time.sleep(60)  # Check every minute
                    
                except Exception as e:
                    print(f"Rebalancing loop error: {e}")
                    time.sleep(60)
        
        thread = threading.Thread(target=rebalance_loop, daemon=True)
        thread.start()
        return thread
    
    def stop_rebalancing_loop(self):
        """Stop the automatic rebalancing loop."""
        self.is_running = False
    
    def get_rebalancing_status(self) -> Dict[str, Any]:
        """Get current rebalancing status and statistics."""
        now = int(time.time())
        
        return {
            "is_running": self.is_running,
            "last_rebalance_ts": self.last_rebalance_ts,
            "next_rebalance_ts": self.calculate_next_rebalance_time(),
            "time_since_last_rebalance_s": now - self.last_rebalance_ts,
            "time_to_next_rebalance_s": max(0, self.calculate_next_rebalance_time() - now),
            "recent_turnover": self.turnover_history[-5:] if self.turnover_history else [],
            "avg_turnover": sum(self.turnover_history) / len(self.turnover_history) if self.turnover_history else 0.0,
            "position_count": len(self.position_history),
            "volatility_multiplier": self._get_volatility_multiplier(),
            "turnover_penalty": self._get_turnover_penalty()
        }


# Global rebalancing manager instance
_rebalancing_manager = None


def get_rebalancing_manager() -> RebalancingManager:
    """Get the global rebalancing manager instance."""
    global _rebalancing_manager
    if _rebalancing_manager is None:
        _rebalancing_manager = RebalancingManager()
    return _rebalancing_manager


def run_rebalancing_cycle(dry_run: bool = False) -> Dict[str, Any]:
    """Run a single rebalancing cycle."""
    manager = get_rebalancing_manager()
    return manager.execute_rebalance(dry_run=dry_run)


def start_automatic_rebalancing() -> threading.Thread:
    """Start automatic rebalancing loop."""
    manager = get_rebalancing_manager()
    return manager.start_rebalancing_loop()


def stop_automatic_rebalancing():
    """Stop automatic rebalancing loop."""
    manager = get_rebalancing_manager()
    manager.stop_rebalancing_loop()


def get_rebalancing_status() -> Dict[str, Any]:
    """Get current rebalancing status."""
    manager = get_rebalancing_manager()
    return manager.get_rebalancing_status()
