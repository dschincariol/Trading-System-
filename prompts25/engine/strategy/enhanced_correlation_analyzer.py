"""
Enhanced Cross-Model Correlation and Crowding Control System

Design goals:
- Measure correlation between models (PnL and factor exposure)
- Detect crowding across assets and horizons
- Penalize correlated strategies in capital allocation
- Prevent hidden concentration risk
- Low latency and stable under live trading
"""

import numpy as np
from typing import Dict, List, Tuple, Set, Optional, Any
from dataclasses import dataclass, field
from collections import defaultdict
import time
import math
from engine.storage import connect


@dataclass
class ModelMetrics:
    """Metrics for a single model/strategy"""
    model_id: str
    strategy_type: str
    asset: str
    horizon: str
    pnl_history: List[float] = field(default_factory=list)
    factor_exposures: Dict[str, float] = field(default_factory=dict)
    current_position: float = 0.0
    daily_turnover: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    volatility: float = 0.0


@dataclass
class CorrelationMetrics:
    """Correlation metrics between models"""
    pnl_correlation: float = 0.0
    factor_correlation: float = 0.0
    asset_overlap: float = 0.0
    horizon_overlap: float = 0.0
    strategy_similarity: float = 0.0
    combined_correlation: float = 0.0


@dataclass
class CrowdingMetrics:
    """Crowding detection metrics"""
    asset_crowding_score: float = 0.0
    horizon_crowding_score: float = 0.0
    strategy_crowding_score: float = 0.0
    sector_concentration: float = 0.0
    liquidity_pressure: float = 0.0
    overall_crowding: float = 0.0


class EnhancedCorrelationAnalyzer:
    """
    Advanced correlation and crowding analysis system for portfolio risk management.
    """
    
    def __init__(self, lookback_days: int = 30, min_observations: int = 10):
        self.lookback_days = lookback_days
        self.min_observations = min_observations
        self.model_metrics: Dict[str, ModelMetrics] = {}
        self.correlation_cache: Dict[Tuple[str, str], CorrelationMetrics] = {}
        self.crowding_cache: Dict[str, CrowdingMetrics] = {}
        self.factor_universe = self._load_factor_universe()
        self.asset_sectors = self._load_asset_sectors()
        self.last_update_ts = 0
        
        # Configuration parameters
        self.config = {
            'pnl_correlation_weight': 0.3,
            'factor_correlation_weight': 0.25,
            'asset_overlap_weight': 0.2,
            'horizon_overlap_weight': 0.15,
            'strategy_similarity_weight': 0.1,
            'crowding_threshold': 0.4,
            'concentration_threshold': 0.3,
            'liquidity_pressure_threshold': 0.5
        }
    
    def _load_factor_universe(self) -> Dict[str, List[str]]:
        """Load factor universe definitions"""
        return {
            'momentum': ['MOM_5d', 'MOM_20d', 'MOM_60d', 'RSI_14d'],
            'value': ['PE_RATIO', 'PB_RATIO', 'PS_RATIO', 'EV_EBITDA'],
            'quality': ['ROE', 'ROA', 'DEBT_EQUITY', 'CURRENT_RATIO'],
            'volatility': ['VOL_10d', 'VOL_20d', 'VOL_60d', 'BETA'],
            'size': ['MARKET_CAP', 'MARKET_CAP_LOG'],
            'macro': ['INTEREST_RATE', 'INFLATION', 'GDP_GROWTH', 'UNEMPLOYMENT']
        }
    
    def _load_asset_sectors(self) -> Dict[str, str]:
        """Load asset sector classifications"""
        return {
            # Technology
            'AAPL': 'Technology', 'MSFT': 'Technology', 'GOOGL': 'Technology', 
            'META': 'Technology', 'NVDA': 'Technology', 'AMD': 'Technology',
            
            # Finance
            'JPM': 'Finance', 'BAC': 'Finance', 'GS': 'Finance', 'MS': 'Finance',
            'C': 'Finance', 'WFC': 'Finance', 'BLK': 'Finance',
            
            # Energy
            'XOM': 'Energy', 'CVX': 'Energy', 'COP': 'Energy', 'EOG': 'Energy',
            'SLB': 'Energy', 'HAL': 'Energy',
            
            # Consumer
            'AMZN': 'Consumer', 'WMT': 'Consumer', 'HD': 'Consumer', 
            'MCD': 'Consumer', 'NKE': 'Consumer', 'TSLA': 'Consumer',
            
            # Healthcare
            'JNJ': 'Healthcare', 'PFE': 'Healthcare', 'UNH': 'Healthcare',
            'ABT': 'Healthcare', 'MRK': 'Healthcare'
        }
    
    def update_model_metrics(self, model_id: str, strategy_type: str, asset: str, 
                           horizon: str, pnl: float = None, factor_exposures: Dict = None) -> None:
        """Update metrics for a specific model"""
        if model_id not in self.model_metrics:
            self.model_metrics[model_id] = ModelMetrics(
                model_id=model_id,
                strategy_type=strategy_type,
                asset=asset,
                horizon=horizon
            )
        
        metrics = self.model_metrics[model_id]
        
        # Update PnL history
        if pnl is not None:
            metrics.pnl_history.append(pnl)
            # Keep only recent history
            if len(metrics.pnl_history) > 100:
                metrics.pnl_history = metrics.pnl_history[-100:]
            
            # Update derived metrics
            self._update_performance_metrics(metrics)
        
        # Update factor exposures
        if factor_exposures:
            metrics.factor_exposures.update(factor_exposures)
    
    def _update_performance_metrics(self, metrics: ModelMetrics) -> None:
        """Update performance-derived metrics"""
        if len(metrics.pnl_history) < 2:
            return
        
        returns = np.array(metrics.pnl_history)
        
        # Sharpe ratio (annualized)
        if len(returns) > 1 and np.std(returns) > 0:
            metrics.sharpe_ratio = np.mean(returns) / np.std(returns) * np.sqrt(252)
        
        # Maximum drawdown
        cumulative = np.cumprod(1 + returns)
        running_max = np.maximum.accumulate(cumulative)
        drawdown = (cumulative - running_max) / running_max
        metrics.max_drawdown = np.min(drawdown) if len(drawdown) > 0 else 0
        
        # Volatility
        metrics.volatility = np.std(returns) * np.sqrt(252) if len(returns) > 1 else 0
    
    def calculate_model_correlation(self, model1: str, model2: str) -> CorrelationMetrics:
        """Calculate comprehensive correlation between two models"""
        cache_key = tuple(sorted([model1, model2]))
        if cache_key in self.correlation_cache:
            return self.correlation_cache[cache_key]
        
        if model1 not in self.model_metrics or model2 not in self.model_metrics:
            return CorrelationMetrics()
        
        m1 = self.model_metrics[model1]
        m2 = self.model_metrics[model2]
        
        metrics = CorrelationMetrics()
        
        # 1. PnL correlation
        metrics.pnl_correlation = self._calculate_pnl_correlation(m1, m2)
        
        # 2. Factor exposure correlation
        metrics.factor_correlation = self._calculate_factor_correlation(m1, m2)
        
        # 3. Asset overlap
        metrics.asset_overlap = self._calculate_asset_overlap(m1, m2)
        
        # 4. Horizon overlap
        metrics.horizon_overlap = self._calculate_horizon_overlap(m1, m2)
        
        # 5. Strategy similarity
        metrics.strategy_similarity = self._calculate_strategy_similarity(m1, m2)
        
        # Combined correlation score
        metrics.combined_correlation = (
            metrics.pnl_correlation * self.config['pnl_correlation_weight'] +
            metrics.factor_correlation * self.config['factor_correlation_weight'] +
            metrics.asset_overlap * self.config['asset_overlap_weight'] +
            metrics.horizon_overlap * self.config['horizon_overlap_weight'] +
            metrics.strategy_similarity * self.config['strategy_similarity_weight']
        )
        
        self.correlation_cache[cache_key] = metrics
        return metrics
    
    def _calculate_pnl_correlation(self, m1: ModelMetrics, m2: ModelMetrics) -> float:
        """Calculate PnL correlation between two models"""
        if len(m1.pnl_history) < self.min_observations or len(m2.pnl_history) < self.min_observations:
            return 0.0
        
        # Align PnL series by length
        min_len = min(len(m1.pnl_history), len(m2.pnl_history))
        pnl1 = np.array(m1.pnl_history[-min_len:])
        pnl2 = np.array(m2.pnl_history[-min_len:])
        
        if len(pnl1) < 2 or np.std(pnl1) == 0 or np.std(pnl2) == 0:
            return 0.0
        
        correlation = np.corrcoef(pnl1, pnl2)[0, 1]
        return correlation if math.isfinite(correlation) else 0.0
    
    def _calculate_factor_correlation(self, m1: ModelMetrics, m2: ModelMetrics) -> float:
        """Calculate factor exposure correlation"""
        if not m1.factor_exposures or not m2.factor_exposures:
            return 0.0
        
        # Find common factors
        common_factors = set(m1.factor_exposures.keys()) & set(m2.factor_exposures.keys())
        if not common_factors:
            return 0.0
        
        # Calculate correlation of factor exposures
        exposures1 = [m1.factor_exposures[f] for f in common_factors]
        exposures2 = [m2.factor_exposures[f] for f in common_factors]
        
        if len(exposures1) < 2:
            return 0.0
        
        correlation = np.corrcoef(exposures1, exposures2)[0, 1]
        return abs(correlation) if math.isfinite(correlation) else 0.0
    
    def _calculate_asset_overlap(self, m1: ModelMetrics, m2: ModelMetrics) -> float:
        """Calculate asset overlap score"""
        if m1.asset == m2.asset:
            return 1.0
        
        # Check if assets are in same sector
        sector1 = self.asset_sectors.get(m1.asset, 'Other')
        sector2 = self.asset_sectors.get(m2.asset, 'Other')
        
        return 0.6 if sector1 == sector2 else 0.0
    
    def _calculate_horizon_overlap(self, m1: ModelMetrics, m2: ModelMetrics) -> float:
        """Calculate horizon overlap score"""
        horizon_order = {'5m': 0, '15m': 1, '1h': 2, '4h': 3, '1d': 4, '3d': 5, '1w': 6, '2w': 7}
        
        h1 = horizon_order.get(m1.horizon, 4)
        h2 = horizon_order.get(m2.horizon, 4)
        
        distance = abs(h1 - h2)
        max_distance = len(horizon_order) - 1
        
        return 1.0 - (distance / max_distance)
    
    def _calculate_strategy_similarity(self, m1: ModelMetrics, m2: ModelMetrics) -> float:
        """Calculate strategy type similarity"""
        if m1.strategy_type == m2.strategy_type:
            return 1.0
        
        # Strategy group similarities
        strategy_groups = {
            'momentum': ['trend', 'breakout', 'momentum'],
            'mean_reversion': ['mean_reversion', 'stat_arb', 'pairs'],
            'volatility': ['vol_arb', 'volatility', 'options'],
            'macro': ['macro', 'currency', 'commodities']
        }
        
        group1 = self._get_strategy_group(m1.strategy_type, strategy_groups)
        group2 = self._get_strategy_group(m2.strategy_type, strategy_groups)
        
        if group1 == group2:
            return 0.7
        elif group1 in ['momentum', 'mean_reversion'] and group2 in ['momentum', 'mean_reversion']:
            return 0.3
        else:
            return 0.1
    
    def _get_strategy_group(self, strategy: str, groups: Dict[str, List[str]]) -> str:
        """Map strategy to its group"""
        for group, strategies in groups.items():
            if any(s in strategy.lower() for s in strategies):
                return group
        return 'other'
    
    def detect_crowding(self, model_id: str) -> CrowdingMetrics:
        """Detect crowding for a specific model"""
        if model_id in self.crowding_cache:
            cached = self.crowding_cache[model_id]
            # Cache for 5 minutes
            if time.time() - self.last_update_ts < 300:
                return cached
        
        if model_id not in self.model_metrics:
            return CrowdingMetrics()
        
        model = self.model_metrics[model_id]
        metrics = CrowdingMetrics()
        
        # Calculate various crowding dimensions
        metrics.asset_crowding_score = self._calculate_asset_crowding(model)
        metrics.horizon_crowding_score = self._calculate_horizon_crowding(model)
        metrics.strategy_crowding_score = self._calculate_strategy_crowding(model)
        metrics.sector_concentration = self._calculate_sector_concentration(model)
        metrics.liquidity_pressure = self._calculate_liquidity_pressure(model)
        
        # Overall crowding score
        metrics.overall_crowding = (
            metrics.asset_crowding_score * 0.3 +
            metrics.horizon_crowding_score * 0.2 +
            metrics.strategy_crowding_score * 0.25 +
            metrics.sector_concentration * 0.15 +
            metrics.liquidity_pressure * 0.1
        )
        
        self.crowding_cache[model_id] = metrics
        self.last_update_ts = time.time()
        
        return metrics
    
    def _calculate_asset_crowding(self, model: ModelMetrics) -> float:
        """Calculate crowding on the same asset"""
        same_asset_models = [
            m for m in self.model_metrics.values() 
            if m.asset == model.asset and m.model_id != model.model_id
        ]
        
        if not same_asset_models:
            return 0.0
        
        # Sum of positions on same asset
        total_exposure = sum(abs(m.current_position) for m in same_asset_models)
        return min(total_exposure / 0.5, 1.0)  # Normalize to 0-1, assuming 50% is max
    
    def _calculate_horizon_crowding(self, model: ModelMetrics) -> float:
        """Calculate crowding in the same horizon"""
        same_horizon_models = [
            m for m in self.model_metrics.values()
            if m.horizon == model.horizon and m.model_id != model.model_id
        ]
        
        if not same_horizon_models:
            return 0.0
        
        # Count models in same horizon
        return min(len(same_horizon_models) / 10.0, 1.0)  # Normalize to 0-1
    
    def _calculate_strategy_crowding(self, model: ModelMetrics) -> float:
        """Calculate crowding of similar strategies"""
        similar_strategies = [
            m for m in self.model_metrics.values()
            if self._calculate_strategy_similarity(model, m) > 0.7 and m.model_id != model.model_id
        ]
        
        if not similar_strategies:
            return 0.0
        
        return min(len(similar_strategies) / 5.0, 1.0)  # Normalize to 0-1
    
    def _calculate_sector_concentration(self, model: ModelMetrics) -> float:
        """Calculate sector concentration risk"""
        sector = self.asset_sectors.get(model.asset, 'Other')
        
        sector_models = [
            m for m in self.model_metrics.values()
            if self.asset_sectors.get(m.asset, 'Other') == sector
        ]
        
        if not sector_models:
            return 0.0
        
        # Total exposure to sector
        total_sector_exposure = sum(abs(m.current_position) for m in sector_models)
        return min(total_sector_exposure / 0.4, 1.0)  # 40% sector cap
    
    def _calculate_liquidity_pressure(self, model: ModelMetrics) -> float:
        """Calculate liquidity pressure based on turnover and position size"""
        # Simple proxy: daily turnover * position size
        pressure = model.daily_turnover * abs(model.current_position)
        return min(pressure / 0.1, 1.0)  # Normalize to 0-1
    
    def calculate_correlation_penalty(self, model_id: str, weight: float) -> float:
        """Calculate correlation penalty for capital allocation"""
        crowding = self.detect_crowding(model_id)
        
        # Base penalty from crowding
        penalty = crowding.overall_crowding
        
        # Additional penalty if above thresholds
        if crowding.overall_crowding > self.config['crowding_threshold']:
            penalty += 0.2
        
        if crowding.sector_concentration > self.config['concentration_threshold']:
            penalty += 0.15
        
        if crowding.liquidity_pressure > self.config['liquidity_pressure_threshold']:
            penalty += 0.1
        
        return min(penalty, 0.5)  # Cap at 50% penalty
    
    def get_portfolio_correlation_matrix(self) -> Dict[Tuple[str, str], float]:
        """Get correlation matrix for all active models"""
        active_models = [m_id for m_id, m in self.model_metrics.items() if len(m.pnl_history) >= self.min_observations]
        
        correlation_matrix = {}
        for i, model1 in enumerate(active_models):
            for model2 in active_models[i:]:
                corr_metrics = self.calculate_model_correlation(model1, model2)
                correlation_matrix[(model1, model2)] = corr_metrics.combined_correlation
                correlation_matrix[(model2, model1)] = corr_metrics.combined_correlation
        
        return correlation_matrix
    
    def get_risk_report(self) -> Dict[str, Any]:
        """Generate comprehensive risk report"""
        active_models = [m_id for m_id, m in self.model_metrics.items() if len(m.pnl_history) >= self.min_observations]
        
        if not active_models:
            return {"error": "No active models with sufficient data"}
        
        # Portfolio-level metrics
        portfolio_correlations = []
        crowding_scores = []
        
        for model_id in active_models:
            crowding = self.detect_crowding(model_id)
            crowding_scores.append(crowding.overall_crowding)
        
        # Calculate pairwise correlations
        for i, model1 in enumerate(active_models):
            for model2 in active_models[i+1:]:
                corr = self.calculate_model_correlation(model1, model2)
                portfolio_correlations.append(corr.combined_correlation)
        
        return {
            "active_models": len(active_models),
            "avg_correlation": np.mean(portfolio_correlations) if portfolio_correlations else 0,
            "max_correlation": np.max(portfolio_correlations) if portfolio_correlations else 0,
            "avg_crowding": np.mean(crowding_scores) if crowding_scores else 0,
            "max_crowding": np.max(crowding_scores) if crowding_scores else 0,
            "high_crowding_models": [m_id for m_id in active_models 
                                   if self.detect_crowding(m_id).overall_crowding > self.config['crowding_threshold']],
            "high_correlation_pairs": [(m1, m2) for i, m1 in enumerate(active_models) 
                                     for m2 in active_models[i+1:] 
                                     if self.calculate_model_correlation(m1, m2).combined_correlation > 0.7]
        }
