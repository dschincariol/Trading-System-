"""
Adaptive Rebalancing System

Performance-based rebalancing with dynamic cadence.
"""

import time
import numpy as np
from typing import Dict, List
from collections import defaultdict


class AdaptiveRebalancer:
    def __init__(self):
        self.performance_window = defaultdict(list)
        self.rebalance_history = []
        
    def should_rebalance(self, allocations: List[Dict]) -> bool:
        """Determine if rebalancing is needed"""
        if not allocations:
            return False
            
        # Check performance drift
        drift_score = self._calculate_performance_drift()
        if drift_score > 0.15:
            return True
            
        # Check time since last rebalance
        if not self.rebalance_history:
            return True
            
        time_since = time.time() - self.rebalance_history[-1]['timestamp']
        base_interval = 300  # 5 minutes
        
        # Adaptive interval based on volatility
        vol_adj = self._get_volatility_adjustment()
        required_interval = base_interval * vol_adj
        
        return time_since >= required_interval
        
    def _calculate_performance_drift(self) -> float:
        """Calculate performance drift across strategies"""
        if not self.performance_window:
            return 0.0
            
        drifts = []
        for strategy, returns in self.performance_window.items():
            if len(returns) >= 2:
                recent = np.mean(returns[-5:])
                historical = np.mean(returns[:-5]) if len(returns) > 5 else recent
                drift = abs(recent - historical) / max(abs(historical), 0.01)
                drifts.append(drift)
                
        return np.mean(drifts) if drifts else 0.0
        
    def _get_volatility_adjustment(self) -> float:
        """Get volatility-based interval adjustment"""
        if not self.performance_window:
            return 1.0
            
        all_returns = []
        for returns in self.performance_window.values():
            all_returns.extend(returns[-10:])
            
        if len(all_returns) < 2:
            return 1.0
            
        vol = np.std(all_returns)
        # Higher volatility = more frequent rebalancing
        return max(0.5, min(2.0, 1.0 / (1.0 + vol)))
