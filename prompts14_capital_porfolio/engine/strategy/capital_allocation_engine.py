"""
Capital Allocation Engine

Design goals:
- Allocate capital across strategies, assets, and horizons
- Penalize correlated risk
- Respect execution capacity limits
- Adapt allocations based on performance
- Maintain stability and avoid overtrading

Initial capital: $500k
"""

import os
import json
import time
import math
import numpy as np
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from collections import defaultdict

from engine.storage import connect
from engine.horizons import list_horizon_specs, horizon_priority
# from engine.strategy_selector import load_strategy_module  # Commented out - module not found
# from engine.position_sizing import calculate_position_size  # Commented out - module not found
# from engine.portfolio_risk_gate import apply_portfolio_risk_gate  # Commented out - module not found
from engine.strategy.correlation_risk_analyzer import CorrelationRiskAnalyzer
from engine.strategy.execution_capacity_manager import ExecutionCapacityManager
from engine.strategy.adaptive_rebalancer import AdaptiveRebalancer
from engine.strategy.risk_constraints import get_default_constraints
from engine.strategy.opportunity_allocation import opportunity_weight


@dataclass
class AllocationTarget:
    strategy: str
    asset: str
    horizon: str
    weight: float
    expected_return: float
    confidence: float
    correlation_penalty: float
    execution_capacity_limit: float = 0.0
    performance_score: float = 1.0


@dataclass
class CapitalConstraints:
    total_capital: float = 500000.0
    max_gross_exposure: float = 1.0
    max_net_exposure: float = 0.6
    max_position_size: float = 0.2
    max_correlation_exposure: float = 0.3
    min_rebalance_interval_s: int = 300
    max_turnover: float = 0.4
    correlation_lookback_days: int = 30
    performance_lookback_days: int = 90
    min_confidence_threshold: float = 0.6
    min_signal_strength: float = 1.0


class CapitalAllocationEngine:
    def __init__(self, constraints: Optional[CapitalConstraints] = None):
        self.constraints = constraints or CapitalConstraints()
        self.last_rebalance_ts = 0
        self.performance_history = defaultdict(list)
        self.correlation_matrix = {}
        self.correlation_analyzer = CorrelationRiskAnalyzer()
        self.capacity_manager = ExecutionCapacityManager()
        self.rebalancer = AdaptiveRebalancer()
        self.risk_constraints = get_default_constraints()
        self.capacity_limits = {}
        self._load_capacity_limits()
        
    def calculate_correlation_penalty(self, targets: List[AllocationTarget]) -> Dict[str, float]:
        """Calculate correlation penalties using historical returns"""
        penalties = {}
        
        if len(targets) < 2:
            return penalties
            
        # Build correlation matrix from historical data
        self._update_correlation_matrix(targets)
        
        # Apply penalties for correlated exposures
        for i, target1 in enumerate(targets):
            key1 = f"{target1.strategy}_{target1.asset}"
            penalty = 0.0
            
            for j, target2 in enumerate(targets):
                if i != j:
                    key2 = f"{target2.strategy}_{target2.asset}"
                    corr = self.correlation_matrix.get((key1, key2), 0.0)
                    
                    # Penalty proportional to correlation and other position size
                    penalty += abs(corr) * target2.weight * 0.5
            
            penalties[key1] = min(penalty, self.constraints.max_correlation_exposure)
                    
        return penalties
    
    def calculate_strategy_performance_score(self, strategy: str) -> float:
        """Calculate performance-based score for strategy allocation"""
        if strategy not in self.performance_history:
            return 1.0
            
        recent_perf = self.performance_history[strategy][-20:]  # Last 20 periods
        if not recent_perf:
            return 1.0
            
        avg_return = np.mean(recent_perf)
        volatility = np.std(recent_perf) if len(recent_perf) > 1 else 0.1
        
        # Sharpe-like score with minimum volatility floor
        score = avg_return / max(volatility, 0.01)
        return max(0.1, min(3.0, score))  # Clamp between 0.1 and 3.0
    
    def _load_capacity_limits(self):
        """Load execution capacity limits from database"""
        con = connect()
        try:
            # Create table if it doesn't exist
            con.execute("""
                CREATE TABLE IF NOT EXISTS execution_capacity (
                    id INTEGER PRIMARY KEY,
                    date TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    daily_capacity REAL NOT NULL,
                    daily_usage REAL DEFAULT 0.0,
                    UNIQUE(date, strategy, asset)
                )
            """)
            
            rows = con.execute("""
                SELECT strategy, asset, daily_capacity
                FROM execution_capacity
                WHERE date = date('now')
            """).fetchall()
            
            for strategy, asset, capacity in rows:
                key = f"{strategy}_{asset}"
                self.capacity_limits[key] = float(capacity or 1_000_000)
        except Exception as e:
            print(f"Warning: Could not load capacity limits: {e}")
        finally:
            con.close()
    
    def _update_correlation_matrix(self, targets: List[AllocationTarget]):
        """Update correlation matrix using historical returns"""
        con = connect()
        try:
            assets = list(set(t.asset for t in targets))
            since_ms = int(time.time() * 1000) - self.constraints.correlation_lookback_days * 86400 * 1000
            
            # Load daily returns for all assets
            returns_data = {}
            for asset in assets:
                rows = con.execute("""
                    SELECT date, daily_return
                    FROM asset_returns
                    WHERE symbol = ? AND ts_ms >= ?
                    ORDER BY date
                """, (asset, since_ms)).fetchall()
                
                if rows:
                    returns_data[asset] = [float(r[1]) for r in rows if r[1] is not None]
            
            # Calculate correlations
            for i, asset1 in enumerate(assets):
                for asset2 in assets[i:]:
                    if asset1 in returns_data and asset2 in returns_data:
                        if len(returns_data[asset1]) > 10 and len(returns_data[asset2]) > 10:
                            corr = np.corrcoef(returns_data[asset1], returns_data[asset2])[0, 1]
                            if math.isfinite(corr):
                                self.correlation_matrix[(asset1, asset2)] = float(corr)
                                self.correlation_matrix[(asset2, asset1)] = float(corr)
        finally:
            con.close()
    
    def allocate_capital(self, alerts: List[Dict]) -> List[AllocationTarget]:
        """Main allocation function"""
        if not self._should_rebalance():
            return []
            
        targets = []
        
        # Process alerts into potential allocations
        for alert in alerts:
            if not self._filter_alert(alert):
                continue
                
            strategy = alert.get('strategy', 'unknown')
            asset = alert.get('symbol', 'unknown')
            horizon = alert.get('horizon', '1h')
            
            # Calculate base position size
            expected_z = alert.get('expected_z', 0)
            confidence = alert.get('confidence', 0)
            
            if abs(expected_z) < 1.0 or confidence < 0.6:
                continue
                
            # Get execution confidence
            exec_conf = self._get_execution_confidence(asset, horizon)
            
            # Get regime multiplier
            regime_mult = self._get_regime_multiplier(strategy, asset)
            
            # Calculate opportunity weight using convex optimization
            raw_weight = opportunity_weight(
                signal_conf=confidence,
                regime_mult=regime_mult,
                exec_conf=exec_conf,
                max_cap=self.constraints.max_position_size,
                min_cap=0.0
            )
            
            # Apply signal strength filter
            if abs(expected_z) < self.constraints.min_signal_strength:
                continue
                
            # Apply confidence filter
            if confidence < self.constraints.min_confidence_threshold:
                continue
            
            # Get strategy performance multiplier
            perf_mult = self.calculate_strategy_performance_score(strategy)
            
            # Final weight with performance adjustment
            final_weight = raw_weight * perf_mult * abs(expected_z)
            
            target = AllocationTarget(
                strategy=strategy,
                asset=asset,
                horizon=horizon,
                weight=final_weight,
                expected_return=expected_z,
                confidence=confidence,
                correlation_penalty=0.0,
                performance_score=perf_mult
            )
            targets.append(target)
        
        # Apply correlation penalties
        penalties = self.calculate_correlation_penalty(targets)
        for target in targets:
            key = f"{target.strategy}_{target.asset}"
            target.correlation_penalty = penalties.get(key, 0.0)
            target.weight *= (1.0 - target.correlation_penalty)
        
        # Normalize weights to respect constraints
        targets = self._normalize_weights(targets)
        
        # Apply capacity limits
        targets = self._apply_capacity_limits(targets)
        
        self.last_rebalance_ts = time.time()
        return targets
    
    def _should_rebalance(self) -> bool:
        """Check if enough time has passed since last rebalance"""
        return (time.time() - self.last_rebalance_ts) >= self.constraints.min_rebalance_interval_s
    
    def _filter_alert(self, alert: Dict) -> bool:
        """Filter alerts based on quality criteria"""
        required_fields = ['strategy', 'symbol', 'expected_z', 'confidence', 'horizon']
        return all(field in alert for field in required_fields)
    
    def _normalize_weights(self, targets: List[AllocationTarget]) -> List[AllocationTarget]:
        """Normalize weights to respect gross exposure limit"""
        if not targets:
            return targets
            
        total_weight = sum(t.weight for t in targets)
        if total_weight <= 0:
            return targets
            
        # Scale to gross exposure cap
        scale_factor = min(1.0, self.constraints.max_gross_exposure / total_weight)
        
        for target in targets:
            target.weight *= scale_factor
            
        return targets
    
    def _apply_capacity_limits(self, targets: List[AllocationTarget]) -> List[AllocationTarget]:
        """Apply position size and capacity limits"""
        for target in targets:
            # Apply max position size
            target.weight = min(target.weight, self.constraints.max_position_size)
            
            # Apply execution capacity limits
            key = f"{target.strategy}_{target.asset}"
            capacity_limit = self.capacity_limits.get(key, float('inf'))
            max_weight_by_capacity = capacity_limit / self.constraints.total_capital
            target.weight = min(target.weight, max_weight_by_capacity)
            target.execution_capacity_limit = max_weight_by_capacity
        
        # Re-normalize after capping
        return self._normalize_weights(targets)
    
    def _get_execution_confidence(self, asset: str, horizon: str) -> float:
        """Get execution confidence based on market conditions"""
        # Simple implementation - could be enhanced with market data
        return 0.8
    
    def _get_regime_multiplier(self, strategy: str, asset: str) -> float:
        """Get regime compatibility multiplier"""
        # Simple implementation - could be enhanced with regime detection
        return 1.0
    
    def update_performance(self, strategy: str, return_pct: float):
        """Update performance history for adaptive allocation"""
        self.performance_history[strategy].append(return_pct)
        # Keep only last 50 observations
        if len(self.performance_history[strategy]) > 50:
            self.performance_history[strategy] = self.performance_history[strategy][-50:]
    
    def get_allocation_summary(self, targets: List[AllocationTarget]) -> Dict[str, Any]:
        """Generate summary statistics for current allocation"""
        if not targets:
            return {"total_weight": 0, "strategies": [], "assets": [], "horizons": []}
            
        strategy_weights = defaultdict(float)
        asset_weights = defaultdict(float)
        horizon_weights = defaultdict(float)
        
        for target in targets:
            strategy_weights[target.strategy] += target.weight
            asset_weights[target.asset] += target.weight
            horizon_weights[target.horizon] += target.weight
        
        return {
            "total_weight": sum(t.weight for t in targets),
            "strategy_allocation": dict(strategy_weights),
            "asset_allocation": dict(asset_weights),
            "horizon_allocation": dict(horizon_weights),
            "correlation_penalty_avg": np.mean([t.correlation_penalty for t in targets]),
            "num_positions": len(targets)
        }


def get_latest_alerts(con=None, limit=50) -> List[Dict]:
    """Get latest alerts from database"""
    if con is None:
        con = connect()
    
    query = """
    SELECT strategy, symbol, expected_z, confidence, horizon, ts_ms, explain_json
    FROM alerts 
    WHERE ts_ms > ? 
    ORDER BY ts_ms DESC 
    LIMIT ?
    """
    
    cutoff_ms = int((time.time() - 6*3600) * 1000)  # Last 6 hours
    cursor = con.execute(query, (cutoff_ms, limit))
    
    alerts = []
    for row in cursor.fetchall():
        alerts.append({
            'strategy': row[0],
            'symbol': row[1], 
            'expected_z': row[2],
            'confidence': row[3],
            'horizon': row[4],
            'ts_ms': row[5],
            'explain_json': row[6] or '{}'
        })
    
    return alerts


def run_capital_allocation() -> List[AllocationTarget]:
    """Main entry point for capital allocation"""
    engine = CapitalAllocationEngine()
    
    con = connect()
    try:
        alerts = get_latest_alerts(con)
        targets = engine.allocate_capital(alerts)
        
        # Log allocation summary
        summary = engine.get_allocation_summary(targets)
        print(f"Allocation summary: {json.dumps(summary, indent=2)}")
        
        return targets
        
    finally:
        con.close()


if __name__ == "__main__":
    run_capital_allocation()
