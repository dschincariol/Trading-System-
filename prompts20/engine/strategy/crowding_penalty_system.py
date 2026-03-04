"""
Crowding Penalty System for Capital Allocation

Integrates with the capital allocation engine to apply penalties for:
- Model correlation risk
- Asset crowding
- Horizon crowding
- Sector concentration
- Liquidity pressure
"""

import numpy as np
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass
from collections import defaultdict
import time
import math

from engine.strategy.enhanced_correlation_analyzer import (
    EnhancedCorrelationAnalyzer, 
    CorrelationMetrics, 
    CrowdingMetrics
)
from engine.storage import connect


@dataclass
class PenaltyConfig:
    """Configuration for penalty calculations"""
    # Correlation penalties
    correlation_threshold: float = 0.6
    correlation_penalty_factor: float = 0.3
    max_correlation_penalty: float = 0.4
    
    # Crowding penalties
    asset_crowding_threshold: float = 0.3
    horizon_crowding_threshold: float = 0.4
    strategy_crowding_threshold: float = 0.5
    
    # Concentration penalties
    sector_concentration_cap: float = 0.35
    max_sector_exposure: float = 0.25
    
    # Liquidity penalties
    liquidity_pressure_threshold: float = 0.6
    liquidity_penalty_factor: float = 0.2
    
    # Dynamic adjustment
    penalty_decay_rate: float = 0.95  # Decay factor for penalties over time
    min_penalty: float = 0.0
    max_total_penalty: float = 0.6


@dataclass
class PenaltyBreakdown:
    """Detailed breakdown of applied penalties"""
    correlation_penalty: float = 0.0
    asset_crowding_penalty: float = 0.0
    horizon_crowding_penalty: float = 0.0
    strategy_crowding_penalty: float = 0.0
    sector_concentration_penalty: float = 0.0
    liquidity_penalty: float = 0.0
    total_penalty: float = 0.0
    penalty_reasons: List[str] = None
    
    def __post_init__(self):
        if self.penalty_reasons is None:
            self.penalty_reasons = []


class CrowdingPenaltySystem:
    """
    Advanced penalty system that applies dynamic penalties based on
    correlation and crowding metrics.
    """
    
    def __init__(self, config: PenaltyConfig = None):
        self.config = config or PenaltyConfig()
        self.correlation_analyzer = EnhancedCorrelationAnalyzer()
        self.penalty_history: Dict[str, List[PenaltyBreakdown]] = defaultdict(list)
        self.portfolio_state: Dict[str, float] = {}  # Current portfolio allocations
        self.last_update_ts = 0
        
        # Track penalty effectiveness
        self.penalty_effectiveness: Dict[str, float] = defaultdict(float)
        
    def update_portfolio_state(self, allocations: Dict[str, float]) -> None:
        """Update current portfolio allocation state"""
        self.portfolio_state = allocations
        self.last_update_ts = time.time()
    
    def calculate_model_penalty(self, model_id: str, proposed_weight: float, 
                              strategy_type: str, asset: str, horizon: str) -> PenaltyBreakdown:
        """
        Calculate comprehensive penalty for a model allocation.
        
        Args:
            model_id: Unique model identifier
            proposed_weight: Proposed allocation weight
            strategy_type: Type of strategy
            asset: Asset symbol
            horizon: Trading horizon
            
        Returns:
            PenaltyBreakdown with detailed penalty components
        """
        breakdown = PenaltyBreakdown()
        
        # Update model metrics in correlation analyzer
        self._update_model_metrics(model_id, strategy_type, asset, horizon)
        
        # Get crowding metrics
        crowding = self.correlation_analyzer.detect_crowding(model_id)
        
        # 1. Correlation penalty
        correlation_penalty = self._calculate_correlation_penalty(model_id, proposed_weight)
        breakdown.correlation_penalty = correlation_penalty
        if correlation_penalty > 0:
            breakdown.penalty_reasons.append(f"High correlation: {correlation_penalty:.3f}")
        
        # 2. Asset crowding penalty
        asset_penalty = self._calculate_asset_crowding_penalty(crowding.asset_crowding_score)
        breakdown.asset_crowding_penalty = asset_penalty
        if asset_penalty > 0:
            breakdown.penalty_reasons.append(f"Asset crowding: {crowding.asset_crowding_score:.3f}")
        
        # 3. Horizon crowding penalty
        horizon_penalty = self._calculate_horizon_crowding_penalty(crowding.horizon_crowding_score)
        breakdown.horizon_crowding_penalty = horizon_penalty
        if horizon_penalty > 0:
            breakdown.penalty_reasons.append(f"Horizon crowding: {crowding.horizon_crowding_score:.3f}")
        
        # 4. Strategy crowding penalty
        strategy_penalty = self._calculate_strategy_crowding_penalty(crowding.strategy_crowding_score)
        breakdown.strategy_crowding_penalty = strategy_penalty
        if strategy_penalty > 0:
            breakdown.penalty_reasons.append(f"Strategy crowding: {crowding.strategy_crowding_score:.3f}")
        
        # 5. Sector concentration penalty
        sector_penalty = self._calculate_sector_concentration_penalty(model_id, asset)
        breakdown.sector_concentration_penalty = sector_penalty
        if sector_penalty > 0:
            breakdown.penalty_reasons.append(f"Sector concentration: {sector_penalty:.3f}")
        
        # 6. Liquidity pressure penalty
        liquidity_penalty = self._calculate_liquidity_penalty(crowding.liquidity_pressure)
        breakdown.liquidity_penalty = liquidity_penalty
        if liquidity_penalty > 0:
            breakdown.penalty_reasons.append(f"Liquidity pressure: {crowding.liquidity_pressure:.3f}")
        
        # Calculate total penalty
        breakdown.total_penalty = min(
            sum([
                breakdown.correlation_penalty,
                breakdown.asset_crowding_penalty,
                breakdown.horizon_crowding_penalty,
                breakdown.strategy_crowding_penalty,
                breakdown.sector_concentration_penalty,
                breakdown.liquidity_penalty
            ]),
            self.config.max_total_penalty
        )
        
        # Store penalty history
        self.penalty_history[model_id].append(breakdown)
        if len(self.penalty_history[model_id]) > 100:  # Keep last 100
            self.penalty_history[model_id] = self.penalty_history[model_id][-100:]
        
        return breakdown
    
    def _update_model_metrics(self, model_id: str, strategy_type: str, asset: str, horizon: str) -> None:
        """Update model metrics in correlation analyzer"""
        # Load recent PnL data
        pnl_data = self._load_model_pnl(model_id)
        factor_data = self._load_model_factor_exposures(model_id)
        
        self.correlation_analyzer.update_model_metrics(
            model_id=model_id,
            strategy_type=strategy_type,
            asset=asset,
            horizon=horizon,
            pnl=pnl_data[-1] if pnl_data else None,
            factor_exposures=factor_data
        )
    
    def _load_model_pnl(self, model_id: str) -> List[float]:
        """Load recent PnL data for model"""
        con = connect()
        try:
            since_ms = int(time.time() * 1000) - 30 * 86400 * 1000  # 30 days
            rows = con.execute("""
                SELECT daily_return
                FROM model_performance
                WHERE model_id = ? AND date_ms >= ?
                ORDER BY date_ms
            """, (model_id, since_ms)).fetchall()
            
            return [float(row[0]) for row in rows if row[0] is not None]
        except Exception as e:
            print(f"Warning: Could not load PnL for {model_id}: {e}")
            return []
        finally:
            con.close()
    
    def _load_model_factor_exposures(self, model_id: str) -> Dict[str, float]:
        """Load factor exposures for model"""
        con = connect()
        try:
            rows = con.execute("""
                SELECT factor_name, exposure
                FROM model_factor_exposures
                WHERE model_id = ?
                ORDER BY updated_ts DESC
                LIMIT 50
            """, (model_id,)).fetchall()
            
            return {row[0]: float(row[1]) for row in rows if row[1] is not None}
        except Exception as e:
            print(f"Warning: Could not load factor exposures for {model_id}: {e}")
            return {}
        finally:
            con.close()
    
    def _calculate_correlation_penalty(self, model_id: str, weight: float) -> float:
        """Calculate correlation-based penalty"""
        # Get correlations with other models
        correlation_sum = 0.0
        correlation_count = 0
        
        for other_model_id in self.portfolio_state:
            if other_model_id != model_id:
                other_weight = self.portfolio_state[other_model_id]
                if other_weight > 0:
                    corr_metrics = self.correlation_analyzer.calculate_model_correlation(model_id, other_model_id)
                    correlation_sum += corr_metrics.combined_correlation * other_weight
                    correlation_count += 1
        
        if correlation_count == 0:
            return 0.0
        
        avg_correlation = correlation_sum / correlation_count
        
        if avg_correlation > self.config.correlation_threshold:
            penalty = (avg_correlation - self.config.correlation_threshold) * self.config.correlation_penalty_factor
            return min(penalty, self.config.max_correlation_penalty)
        
        return 0.0
    
    def _calculate_asset_crowding_penalty(self, crowding_score: float) -> float:
        """Calculate asset crowding penalty"""
        if crowding_score > self.config.asset_crowding_threshold:
            return (crowding_score - self.config.asset_crowding_threshold) * 0.5
        return 0.0
    
    def _calculate_horizon_crowding_penalty(self, crowding_score: float) -> float:
        """Calculate horizon crowding penalty"""
        if crowding_score > self.config.horizon_crowding_threshold:
            return (crowding_score - self.config.horizon_crowding_threshold) * 0.4
        return 0.0
    
    def _calculate_strategy_crowding_penalty(self, crowding_score: float) -> float:
        """Calculate strategy crowding penalty"""
        if crowding_score > self.config.strategy_crowding_threshold:
            return (crowding_score - self.config.strategy_crowding_threshold) * 0.6
        return 0.0
    
    def _calculate_sector_concentration_penalty(self, model_id: str, asset: str) -> float:
        """Calculate sector concentration penalty"""
        sector = self.correlation_analyzer.asset_sectors.get(asset, 'Other')
        
        # Calculate current sector exposure
        sector_exposure = 0.0
        for other_model_id, weight in self.portfolio_state.items():
            if other_model_id != model_id and weight > 0:
                other_model = self.correlation_analyzer.model_metrics.get(other_model_id)
                if other_model:
                    other_sector = self.correlation_analyzer.asset_sectors.get(other_model.asset, 'Other')
                    if other_sector == sector:
                        sector_exposure += weight
        
        # Add proposed weight
        proposed_weight = self.portfolio_state.get(model_id, 0)
        total_sector_exposure = sector_exposure + proposed_weight
        
        if total_sector_exposure > self.config.sector_concentration_cap:
            excess = total_sector_exposure - self.config.sector_concentration_cap
            return excess * 2.0  # Heavy penalty for exceeding sector cap
        
        return 0.0
    
    def _calculate_liquidity_penalty(self, liquidity_pressure: float) -> float:
        """Calculate liquidity pressure penalty"""
        if liquidity_pressure > self.config.liquidity_pressure_threshold:
            excess = liquidity_pressure - self.config.liquidity_pressure_threshold
            return excess * self.config.liquidity_penalty_factor
        return 0.0
    
    def apply_penalties_to_allocations(self, allocations: List[Dict]) -> List[Dict]:
        """
        Apply crowding and correlation penalties to a list of allocations.
        
        Args:
            allocations: List of allocation dictionaries with keys:
                - model_id, strategy, asset, horizon, weight, expected_return, confidence
                
        Returns:
            List of allocations with penalty-adjusted weights
        """
        # Update portfolio state with current allocations
        current_state = {alloc.get('model_id', f"{alloc.get('strategy')}_{alloc.get('asset')}"): 
                        alloc.get('weight', 0) for alloc in allocations}
        self.update_portfolio_state(current_state)
        
        penalized_allocations = []
        
        for alloc in allocations:
            model_id = alloc.get('model_id', f"{alloc.get('strategy')}_{alloc.get('asset')}")
            strategy = alloc.get('strategy', 'unknown')
            asset = alloc.get('asset', 'unknown')
            horizon = alloc.get('horizon', '1h')
            weight = alloc.get('weight', 0)
            
            # Calculate penalty
            penalty_breakdown = self.calculate_model_penalty(
                model_id, weight, strategy, asset, horizon
            )
            
            # Apply penalty to weight
            penalty_factor = 1.0 - penalty_breakdown.total_penalty
            adjusted_weight = weight * penalty_factor
            
            # Create penalized allocation
            penalized_alloc = alloc.copy()
            penalized_alloc.update({
                'original_weight': weight,
                'penalty_adjusted_weight': adjusted_weight,
                'penalty_breakdown': penalty_breakdown,
                'penalty_applied': penalty_breakdown.total_penalty > 0,
                'penalty_reasons': penalty_breakdown.penalty_reasons
            })
            
            penalized_allocations.append(penalized_alloc)
        
        return penalized_allocations
    
    def get_penalty_statistics(self) -> Dict[str, Any]:
        """Get statistics on applied penalties"""
        if not self.penalty_history:
            return {"message": "No penalty history available"}
        
        all_penalties = []
        penalty_types = defaultdict(list)
        
        for model_penalties in self.penalty_history.values():
            for breakdown in model_penalties:
                all_penalties.append(breakdown.total_penalty)
                penalty_types['correlation'].append(breakdown.correlation_penalty)
                penalty_types['asset_crowding'].append(breakdown.asset_crowding_penalty)
                penalty_types['horizon_crowding'].append(breakdown.horizon_crowding_penalty)
                penalty_types['strategy_crowding'].append(breakdown.strategy_crowding_penalty)
                penalty_types['sector_concentration'].append(breakdown.sector_concentration_penalty)
                penalty_types['liquidity'].append(breakdown.liquidity_penalty)
        
        stats = {}
        for penalty_type, penalties in penalty_types.items():
            if penalties:
                stats[f'{penalty_type}_avg'] = np.mean(penalties)
                stats[f'{penalty_type}_max'] = np.max(penalties)
                stats[f'{penalty_type}_count'] = sum(1 for p in penalties if p > 0)
        
        if all_penalties:
            stats.update({
                'total_penalties_avg': np.mean(all_penalties),
                'total_penalties_max': np.max(all_penalties),
                'total_penalties_count': sum(1 for p in all_penalties if p > 0),
                'models_penalized': len([m for m, penalties in self.penalty_history.items() 
                                       if any(p.total_penalty > 0 for p in penalties)])
            })
        
        return stats
    
    def get_high_risk_models(self) -> List[Dict[str, Any]]:
        """Identify models with high risk due to crowding/correlation"""
        high_risk = []
        
        for model_id in self.correlation_analyzer.model_metrics:
            crowding = self.correlation_analyzer.detect_crowding(model_id)
            
            # Check if model exceeds any risk thresholds
            risk_factors = []
            if crowding.overall_crowding > 0.5:
                risk_factors.append(f"High crowding: {crowding.overall_crowding:.3f}")
            if crowding.sector_concentration > 0.4:
                risk_factors.append(f"High sector concentration: {crowding.sector_concentration:.3f}")
            if crowding.liquidity_pressure > 0.6:
                risk_factors.append(f"High liquidity pressure: {crowding.liquidity_pressure:.3f}")
            
            # Check recent penalties
            recent_penalties = self.penalty_history.get(model_id, [])[-5:]  # Last 5 penalties
            if recent_penalties:
                avg_penalty = np.mean([p.total_penalty for p in recent_penalties])
                if avg_penalty > 0.3:
                    risk_factors.append(f"High average penalty: {avg_penalty:.3f}")
            
            if risk_factors:
                model_metrics = self.correlation_analyzer.model_metrics[model_id]
                high_risk.append({
                    'model_id': model_id,
                    'strategy': model_metrics.strategy_type,
                    'asset': model_metrics.asset,
                    'horizon': model_metrics.horizon,
                    'risk_factors': risk_factors,
                    'crowding_score': crowding.overall_crowding,
                    'recent_avg_penalty': avg_penalty if recent_penalties else 0
                })
        
        # Sort by overall risk
        high_risk.sort(key=lambda x: x['crowding_score'], reverse=True)
        return high_risk
    
    def adjust_config_for_regime(self, regime: str) -> None:
        """Adjust penalty configuration based on market regime"""
        if regime == "high_volatility":
            self.config.correlation_penalty_factor *= 1.5
            self.config.liquidity_penalty_factor *= 1.3
            self.config.max_total_penalty *= 1.2
        elif regime == "low_volatility":
            self.config.correlation_penalty_factor *= 0.8
            self.config.liquidity_penalty_factor *= 0.9
        elif regime == "crisis":
            self.config.correlation_penalty_factor *= 2.0
            self.config.liquidity_penalty_factor *= 2.0
            self.config.max_total_penalty = min(0.8, self.config.max_total_penalty * 1.5)
