"""
Advanced Correlation Risk Analyzer

Calculates correlation penalties across strategies, assets, and horizons
to reduce portfolio risk from clustered exposures.
"""

import numpy as np
from typing import Dict, List, Tuple, Set, Optional
from collections import defaultdict
from engine.storage import connect


class CorrelationRiskAnalyzer:
    def __init__(self, lookback_days=30):
        self.lookback_days = lookback_days
        self.correlation_cache = {}
        
    def calculate_strategy_correlation_matrix(self, strategies: List[str]) -> Dict[Tuple[str, str], float]:
        """Calculate correlation matrix between strategies"""
        # Simplified correlation based on strategy type and asset overlap
        correlations = {}
        
        # Strategy group mappings (simplified)
        strategy_groups = {
            'trend': ['momentum', 'trend_following', 'breakout'],
            'mean_reversion': ['mean_reversion', 'stat_arb', 'pairs'],
            'volatility': ['vol_arb', 'volatility_scaling', 'options'],
            'macro': ['macro', 'currency', 'commodities']
        }
        
        for i, s1 in enumerate(strategies):
            for s2 in strategies[i+1:]:
                group1 = self._get_strategy_group(s1, strategy_groups)
                group2 = self._get_strategy_group(s2, strategy_groups)
                
                if group1 == group2:
                    corr = 0.6  # High correlation within group
                elif group1 in ['trend', 'mean_reversion'] and group2 in ['trend', 'mean_reversion']:
                    corr = 0.3  # Medium correlation
                else:
                    corr = 0.1  # Low correlation
                    
                correlations[(s1, s2)] = corr
                correlations[(s2, s1)] = corr
                
        return correlations
    
    def calculate_asset_correlation_penalty(self, assets: List[str]) -> Dict[str, float]:
        """Calculate correlation penalties for asset clusters"""
        penalties = {}
        
        # Asset class mappings
        asset_classes = {
            'tech': ['AAPL', 'MSFT', 'GOOGL', 'META', 'NVDA'],
            'finance': ['JPM', 'BAC', 'GS', 'MS', 'C'],
            'energy': ['XOM', 'CVX', 'COP', 'EOG', 'SLB'],
            'consumer': ['AMZN', 'WMT', 'HD', 'MCD', 'NKE']
        }
        
        asset_to_class = {}
        for cls, symbols in asset_classes.items():
            for symbol in symbols:
                asset_to_class[symbol] = cls
        
        # Count exposures per asset class
        class_counts = defaultdict(int)
        for asset in assets:
            cls = asset_to_class.get(asset, 'other')
            class_counts[cls] += 1
        
        # Apply penalties for clustered exposure
        for asset in assets:
            cls = asset_to_class.get(asset, 'other')
            if class_counts[cls] > 1:
                penalty = 0.15 * (class_counts[cls] - 1)
                penalties[asset] = min(penalty, 0.4)  # Cap at 40%
                
        return penalties
    
    def _get_strategy_group(self, strategy: str, groups: Dict[str, List[str]]) -> str:
        """Map strategy to its group"""
        for group, strategies in groups.items():
            if any(s in strategy.lower() for s in strategies):
                return group
        return 'other'
    
    def calculate_portfolio_correlation_risk(self, allocations: List[Dict]) -> float:
        """Calculate overall portfolio correlation risk score"""
        if len(allocations) < 2:
            return 0.0
            
        strategies = [a.get('strategy', '') for a in allocations]
        assets = [a.get('asset', '') for a in allocations]
        
        # Get correlation matrices
        strategy_corr = self.calculate_strategy_correlation_matrix(strategies)
        asset_penalties = self.calculate_asset_correlation_penalty(assets)
        
        # Calculate weighted correlation risk
        total_risk = 0.0
        total_weight = 0.0
        
        for i, alloc1 in enumerate(allocations):
            for j, alloc2 in enumerate(allocations[i+1:], i+1):
                w1 = alloc1.get('weight', 0)
                w2 = alloc2.get('weight', 0)
                
                # Strategy correlation
                strat_corr = strategy_corr.get((alloc1['strategy'], alloc2['strategy']), 0.1)
                
                # Asset penalty
                asset_penalty = asset_penalties.get(alloc1['asset'], 0) + asset_penalties.get(alloc2['asset'], 0)
                
                # Combined risk contribution
                pair_risk = w1 * w2 * (strat_corr + asset_penalty)
                total_risk += pair_risk
                total_weight += w1 * w2
                
        return total_risk if total_weight > 0 else 0.0
