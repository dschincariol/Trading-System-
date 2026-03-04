"""
Concentration Risk Prevention System

Prevents hidden concentration risk through:
- Sector concentration limits
- Asset concentration limits
- Strategy concentration limits
- Horizon concentration limits
- Factor exposure concentration limits
- Dynamic risk budgeting
"""

import numpy as np
from typing import Dict, List, Tuple, Optional, Any, Set
from dataclasses import dataclass, field
from collections import defaultdict
import time
import math
from enum import Enum

from engine.storage import connect


class RiskLevel(Enum):
    """Risk level classification"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ConcentrationLimits:
    """Concentration limits configuration"""
    # Sector limits
    max_sector_exposure: float = 0.30  # Max 30% in any sector
    max_sector_positions: int = 8       # Max positions in any sector
    
    # Asset limits
    max_single_asset_exposure: float = 0.08  # Max 8% in single asset
    max_asset_positions: int = 3            # Max positions per asset
    
    # Strategy limits
    max_strategy_exposure: float = 0.25  # Max 25% in any strategy type
    max_strategy_positions: int = 5      # Max positions per strategy
    
    # Horizon limits
    max_horizon_exposure: float = 0.35  # Max 35% in any horizon
    max_horizon_positions: int = 6      # Max positions per horizon
    
    # Factor limits
    max_factor_exposure: float = 0.20   # Max 20% exposure to any single factor
    max_factor_positions: int = 4       # Max positions with same factor bias
    
    # Portfolio limits
    max_total_positions: int = 25       # Max total positions
    min_diversification_score: float = 0.6  # Minimum diversification score


@dataclass
class ConcentrationMetrics:
    """Current concentration metrics"""
    sector_exposures: Dict[str, float] = field(default_factory=dict)
    asset_exposures: Dict[str, float] = field(default_factory=dict)
    strategy_exposures: Dict[str, float] = field(default_factory=dict)
    horizon_exposures: Dict[str, float] = field(default_factory=dict)
    factor_exposures: Dict[str, float] = field(default_factory=dict)
    
    sector_counts: Dict[str, int] = field(default_factory=dict)
    asset_counts: Dict[str, int] = field(default_factory=dict)
    strategy_counts: Dict[str, int] = field(default_factory=dict)
    horizon_counts: Dict[str, int] = field(default_factory=dict)
    factor_counts: Dict[str, int] = field(default_factory=dict)
    
    diversification_score: float = 0.0
    concentration_risk_level: RiskLevel = RiskLevel.LOW
    total_positions: int = 0


@dataclass
class RiskAlert:
    """Risk alert for concentration violations"""
    alert_type: str
    severity: RiskLevel
    description: str
    current_value: float
    limit_value: float
    affected_entities: List[str]
    timestamp: float
    recommended_action: str


class ConcentrationRiskController:
    """
    Advanced concentration risk prevention system that monitors
    and enforces diversification constraints.
    """
    
    def __init__(self, limits: ConcentrationLimits = None):
        self.limits = limits or ConcentrationLimits()
        self.current_metrics = ConcentrationMetrics()
        self.risk_alerts: List[RiskAlert] = []
        self.alert_history: List[RiskAlert] = []
        self.portfolio_allocations: Dict[str, Dict] = {}
        self.last_update_ts = 0
        
        # Asset and sector mappings
        self.asset_sectors = self._load_asset_sectors()
        self.strategy_groups = self._load_strategy_groups()
        self.hierarchy_weights = self._load_hierarchy_weights()
        
        # Risk budgeting
        self.risk_budget_used: Dict[str, float] = defaultdict(float)
        self.risk_budget_total: Dict[str, float] = {
            'sector': 1.0,
            'asset': 1.0,
            'strategy': 1.0,
            'horizon': 1.0,
            'factor': 1.0
        }
    
    def _load_asset_sectors(self) -> Dict[str, str]:
        """Load asset sector classifications"""
        return {
            # Technology
            'AAPL': 'Technology', 'MSFT': 'Technology', 'GOOGL': 'Technology', 
            'META': 'Technology', 'NVDA': 'Technology', 'AMD': 'Technology',
            'INTC': 'Technology', 'CSCO': 'Technology', 'ORCL': 'Technology',
            
            # Finance
            'JPM': 'Finance', 'BAC': 'Finance', 'GS': 'Finance', 'MS': 'Finance',
            'C': 'Finance', 'WFC': 'Finance', 'BLK': 'Finance', 'AXP': 'Finance',
            
            # Energy
            'XOM': 'Energy', 'CVX': 'Energy', 'COP': 'Energy', 'EOG': 'Energy',
            'SLB': 'Energy', 'HAL': 'Energy', 'PSX': 'Energy', 'VLO': 'Energy',
            
            # Consumer
            'AMZN': 'Consumer', 'WMT': 'Consumer', 'HD': 'Consumer', 
            'MCD': 'Consumer', 'NKE': 'Consumer', 'TSLA': 'Consumer',
            'LOW': 'Consumer', 'TGT': 'Consumer', 'COST': 'Consumer',
            
            # Healthcare
            'JNJ': 'Healthcare', 'PFE': 'Healthcare', 'UNH': 'Healthcare',
            'ABT': 'Healthcare', 'MRK': 'Healthcare', 'TMO': 'Healthcare',
            
            # Industrial
            'CAT': 'Industrial', 'GE': 'Industrial', 'MMM': 'Industrial',
            'HON': 'Industrial', 'UPS': 'Industrial', 'BA': 'Industrial',
            
            # Materials
            'LIN': 'Materials', 'APD': 'Materials', 'ECL': 'Materials',
            'SHW': 'Materials', 'DOW': 'Materials', 'DD': 'Materials'
        }
    
    def _load_strategy_groups(self) -> Dict[str, List[str]]:
        """Load strategy group classifications"""
        return {
            'momentum': ['momentum', 'trend', 'breakout', 'trend_following'],
            'mean_reversion': ['mean_reversion', 'stat_arb', 'pairs', 'reversal'],
            'volatility': ['vol_arb', 'volatility', 'options', 'variance'],
            'macro': ['macro', 'currency', 'commodities', 'fixed_income'],
            'fundamental': ['fundamental', 'value', 'growth', 'quality'],
            'quantitative': ['quantitative', 'statistical', 'machine_learning', 'ai']
        }
    
    def _load_hierarchy_weights(self) -> Dict[str, float]:
        """Load hierarchy importance weights"""
        return {
            'sector': 0.3,
            'asset': 0.25,
            'strategy': 0.2,
            'horizon': 0.15,
            'factor': 0.1
        }
    
    def update_portfolio(self, allocations: List[Dict]) -> None:
        """
        Update portfolio state and recalculate concentration metrics.
        
        Args:
            allocations: List of allocation dictionaries
        """
        self.portfolio_allocations = {
            alloc.get('model_id', f"{alloc.get('strategy')}_{alloc.get('asset')}"): alloc 
            for alloc in allocations
        }
        
        self._calculate_concentration_metrics()
        self._detect_concentration_violations()
        self.last_update_ts = time.time()
    
    def _calculate_concentration_metrics(self) -> None:
        """Calculate current concentration metrics"""
        # Reset metrics
        self.current_metrics = ConcentrationMetrics()
        
        if not self.portfolio_allocations:
            return
        
        # Calculate exposures and counts
        for alloc_id, alloc in self.portfolio_allocations.items():
            weight = alloc.get('weight', 0)
            if weight <= 0:
                continue
            
            strategy = alloc.get('strategy', 'unknown')
            asset = alloc.get('asset', 'unknown')
            horizon = alloc.get('horizon', '1h')
            
            # Sector classification
            sector = self.asset_sectors.get(asset, 'Other')
            self.current_metrics.sector_exposures[sector] = self.current_metrics.sector_exposures.get(sector, 0) + weight
            self.current_metrics.sector_counts[sector] = self.current_metrics.sector_counts.get(sector, 0) + 1
            
            # Asset exposure
            self.current_metrics.asset_exposures[asset] = self.current_metrics.asset_exposures.get(asset, 0) + weight
            self.current_metrics.asset_counts[asset] = self.current_metrics.asset_counts.get(asset, 0) + 1
            
            # Strategy classification
            strategy_group = self._classify_strategy(strategy)
            self.current_metrics.strategy_exposures[strategy_group] = self.current_metrics.strategy_exposures.get(strategy_group, 0) + weight
            self.current_metrics.strategy_counts[strategy_group] = self.current_metrics.strategy_counts.get(strategy_group, 0) + 1
            
            # Horizon exposure
            self.current_metrics.horizon_exposures[horizon] = self.current_metrics.horizon_exposures.get(horizon, 0) + weight
            self.current_metrics.horizon_counts[horizon] = self.current_metrics.horizon_counts.get(horizon, 0) + 1
            
            # Factor exposures (simplified - would load from database)
            factor_bias = self._get_primary_factor_bias(strategy)
            if factor_bias:
                self.current_metrics.factor_exposures[factor_bias] = self.current_metrics.factor_exposures.get(factor_bias, 0) + weight
                self.current_metrics.factor_counts[factor_bias] = self.current_metrics.factor_counts.get(factor_bias, 0) + 1
        
        self.current_metrics.total_positions = len(self.portfolio_allocations)
        self.current_metrics.diversification_score = self._calculate_diversification_score()
        self.current_metrics.concentration_risk_level = self._assess_risk_level()
    
    def _classify_strategy(self, strategy: str) -> str:
        """Classify strategy into group"""
        strategy_lower = strategy.lower()
        for group, strategies in self.strategy_groups.items():
            if any(s in strategy_lower for s in strategies):
                return group
        return 'other'
    
    def _get_primary_factor_bias(self, strategy: str) -> Optional[str]:
        """Get primary factor bias for strategy (simplified)"""
        strategy_lower = strategy.lower()
        if any(word in strategy_lower for word in ['momentum', 'trend']):
            return 'momentum'
        elif any(word in strategy_lower for word in ['value', 'fundamental']):
            return 'value'
        elif any(word in strategy_lower for word in ['volatility', 'vol']):
            return 'volatility'
        elif any(word in strategy_lower for word in ['quality', 'roe', 'roa']):
            return 'quality'
        elif any(word in strategy_lower for word in ['size', 'market_cap']):
            return 'size'
        return None
    
    def _calculate_diversification_score(self) -> float:
        """Calculate overall diversification score"""
        if not self.portfolio_allocations:
            return 1.0
        
        # Calculate Herfindahl-Hirschman Index (HHI) for each dimension
        hhi_scores = []
        
        # Sector HHI
        if self.current_metrics.sector_exposures:
            sector_weights = list(self.current_metrics.sector_exposures.values())
            sector_hhi = sum(w**2 for w in sector_weights)
            hhi_scores.append(1 - sector_hhi)  # Convert to diversification score
        
        # Asset HHI
        if self.current_metrics.asset_exposures:
            asset_weights = list(self.current_metrics.asset_exposures.values())
            asset_hhi = sum(w**2 for w in asset_weights)
            hhi_scores.append(1 - asset_hhi)
        
        # Strategy HHI
        if self.current_metrics.strategy_exposures:
            strategy_weights = list(self.current_metrics.strategy_exposures.values())
            strategy_hhi = sum(w**2 for w in strategy_weights)
            hhi_scores.append(1 - strategy_hhi)
        
        # Horizon HHI
        if self.current_metrics.horizon_exposures:
            horizon_weights = list(self.current_metrics.horizon_exposures.values())
            horizon_hhi = sum(w**2 for w in horizon_weights)
            hhi_scores.append(1 - horizon_hhi)
        
        # Weighted average
        if hhi_scores:
            weighted_score = sum(score * weight for score, weight in 
                                zip(hhi_scores, list(self.hierarchy_weights.values())[:len(hhi_scores)]))
            return max(0, min(1, weighted_score))
        
        return 1.0
    
    def _assess_risk_level(self) -> RiskLevel:
        """Assess overall concentration risk level"""
        risk_score = 0.0
        
        # Check sector concentration
        max_sector_exp = max(self.current_metrics.sector_exposures.values()) if self.current_metrics.sector_exposures else 0
        if max_sector_exp > 0.4:
            risk_score += 0.3
        elif max_sector_exp > 0.3:
            risk_score += 0.2
        
        # Check asset concentration
        max_asset_exp = max(self.current_metrics.asset_exposures.values()) if self.current_metrics.asset_exposures else 0
        if max_asset_exp > 0.1:
            risk_score += 0.25
        elif max_asset_exp > 0.08:
            risk_score += 0.15
        
        # Check strategy concentration
        max_strategy_exp = max(self.current_metrics.strategy_exposures.values()) if self.current_metrics.strategy_exposures else 0
        if max_strategy_exp > 0.35:
            risk_score += 0.2
        elif max_strategy_exp > 0.25:
            risk_score += 0.1
        
        # Check diversification score
        if self.current_metrics.diversification_score < 0.4:
            risk_score += 0.25
        elif self.current_metrics.diversification_score < 0.6:
            risk_score += 0.1
        
        # Determine risk level
        if risk_score >= 0.7:
            return RiskLevel.CRITICAL
        elif risk_score >= 0.5:
            return RiskLevel.HIGH
        elif risk_score >= 0.3:
            return RiskLevel.MEDIUM
        else:
            return RiskLevel.LOW
    
    def _detect_concentration_violations(self) -> None:
        """Detect concentration limit violations"""
        self.risk_alerts = []
        
        # Check sector limits
        for sector, exposure in self.current_metrics.sector_exposures.items():
            count = self.current_metrics.sector_counts.get(sector, 0)
            
            if exposure > self.limits.max_sector_exposure:
                self.risk_alerts.append(RiskAlert(
                    alert_type="sector_exposure",
                    severity=RiskLevel.HIGH if exposure > self.limits.max_sector_exposure * 1.2 else RiskLevel.MEDIUM,
                    description=f"Sector exposure exceeds limit",
                    current_value=exposure,
                    limit_value=self.limits.max_sector_exposure,
                    affected_entities=[sector],
                    timestamp=time.time(),
                    recommended_action="Reduce sector exposure or add positions in other sectors"
                ))
            
            if count > self.limits.max_sector_positions:
                self.risk_alerts.append(RiskAlert(
                    alert_type="sector_count",
                    severity=RiskLevel.MEDIUM,
                    description=f"Too many positions in sector",
                    current_value=count,
                    limit_value=self.limits.max_sector_positions,
                    affected_entities=[sector],
                    timestamp=time.time(),
                    recommended_action="Reduce number of positions in this sector"
                ))
        
        # Check asset limits
        for asset, exposure in self.current_metrics.asset_exposures.items():
            count = self.current_metrics.asset_counts.get(asset, 0)
            
            if exposure > self.limits.max_single_asset_exposure:
                self.risk_alerts.append(RiskAlert(
                    alert_type="asset_exposure",
                    severity=RiskLevel.CRITICAL if exposure > self.limits.max_single_asset_exposure * 1.5 else RiskLevel.HIGH,
                    description=f"Single asset exposure exceeds limit",
                    current_value=exposure,
                    limit_value=self.limits.max_single_asset_exposure,
                    affected_entities=[asset],
                    timestamp=time.time(),
                    recommended_action="Significantly reduce position size in this asset"
                ))
            
            if count > self.limits.max_asset_positions:
                self.risk_alerts.append(RiskAlert(
                    alert_type="asset_count",
                    severity=RiskLevel.MEDIUM,
                    description=f"Too many positions in same asset",
                    current_value=count,
                    limit_value=self.limits.max_asset_positions,
                    affected_entities=[asset],
                    timestamp=time.time(),
                    recommended_action="Consolidate positions in this asset"
                ))
        
        # Check strategy limits
        for strategy, exposure in self.current_metrics.strategy_exposures.items():
            count = self.current_metrics.strategy_counts.get(strategy, 0)
            
            if exposure > self.limits.max_strategy_exposure:
                self.risk_alerts.append(RiskAlert(
                    alert_type="strategy_exposure",
                    severity=RiskLevel.HIGH,
                    description=f"Strategy exposure exceeds limit",
                    current_value=exposure,
                    limit_value=self.limits.max_strategy_exposure,
                    affected_entities=[strategy],
                    timestamp=time.time(),
                    recommended_action="Reduce exposure to this strategy type"
                ))
        
        # Check horizon limits
        for horizon, exposure in self.current_metrics.horizon_exposures.items():
            if exposure > self.limits.max_horizon_exposure:
                self.risk_alerts.append(RiskAlert(
                    alert_type="horizon_exposure",
                    severity=RiskLevel.MEDIUM,
                    description=f"Horizon exposure exceeds limit",
                    current_value=exposure,
                    limit_value=self.limits.max_horizon_exposure,
                    affected_entities=[horizon],
                    timestamp=time.time(),
                    recommended_action="Distribute exposure across different horizons"
                ))
        
        # Check diversification score
        if self.current_metrics.diversification_score < self.limits.min_diversification_score:
            self.risk_alerts.append(RiskAlert(
                alert_type="diversification",
                severity=RiskLevel.HIGH if self.current_metrics.diversification_score < 0.4 else RiskLevel.MEDIUM,
                description=f"Portfolio diversification below minimum",
                current_value=self.current_metrics.diversification_score,
                limit_value=self.limits.min_diversification_score,
                affected_entities=["portfolio"],
                timestamp=time.time(),
                recommended_action="Add positions in underrepresented sectors/assets"
            ))
        
        # Check total positions
        if self.current_metrics.total_positions > self.limits.max_total_positions:
            self.risk_alerts.append(RiskAlert(
                alert_type="position_count",
                severity=RiskLevel.MEDIUM,
                description=f"Too many total positions",
                current_value=self.current_metrics.total_positions,
                limit_value=self.limits.max_total_positions,
                affected_entities=["portfolio"],
                timestamp=time.time(),
                recommended_action="Reduce number of positions or increase position sizes"
            ))
        
        # Add to alert history
        self.alert_history.extend(self.risk_alerts)
        if len(self.alert_history) > 1000:  # Keep last 1000 alerts
            self.alert_history = self.alert_history[-1000:]
    
    def check_allocation_allowed(self, allocation: Dict) -> Tuple[bool, List[str]]:
        """
        Check if a new allocation would violate concentration limits.
        
        Args:
            allocation: New allocation to check
            
        Returns:
            Tuple of (allowed, list_of_violations)
        """
        violations = []
        
        # Create temporary portfolio with new allocation
        temp_portfolio = self.portfolio_allocations.copy()
        model_id = allocation.get('model_id', f"{allocation.get('strategy')}_{allocation.get('asset')}")
        temp_portfolio[model_id] = allocation
        
        # Temporarily update metrics
        original_metrics = self.current_metrics
        self.portfolio_allocations = temp_portfolio
        self._calculate_concentration_metrics()
        
        # Check for violations
        temp_alerts = self.risk_alerts.copy()
        self._detect_concentration_violations()
        
        # Identify new violations
        new_violations = [alert for alert in self.risk_alerts if alert not in temp_alerts]
        
        for alert in new_violations:
            if alert.severity in [RiskLevel.HIGH, RiskLevel.CRITICAL]:
                violations.append(f"{alert.alert_type}: {alert.description}")
        
        # Restore original state
        self.portfolio_allocations = {k: v for k, v in temp_portfolio.items() if k != model_id}
        self.current_metrics = original_metrics
        self.risk_alerts = temp_alerts
        
        return len(violations) == 0, violations
    
    def suggest_rebalancing_actions(self) -> List[Dict[str, Any]]:
        """Suggest actions to reduce concentration risk"""
        actions = []
        
        # For each alert, suggest specific actions
        for alert in self.risk_alerts:
            if alert.alert_type == "sector_exposure":
                sector = alert.affected_entities[0]
                current_exp = alert.current_value
                reduction_needed = current_exp - self.limits.max_sector_exposure
                
                # Find positions in this sector to reduce
                sector_positions = [
                    (model_id, alloc) for model_id, alloc in self.portfolio_allocations.items()
                    if self.asset_sectors.get(alloc.get('asset'), 'Other') == sector
                ]
                
                if sector_positions:
                    # Suggest reducing largest positions first
                    sector_positions.sort(key=lambda x: x[1].get('weight', 0), reverse=True)
                    
                    actions.append({
                        'type': 'reduce_sector_exposure',
                        'priority': 'high' if alert.severity == RiskLevel.HIGH else 'medium',
                        'description': f"Reduce {sector} sector exposure by {reduction_needed:.3f}",
                        'target_positions': sector_positions[:3],  # Top 3 positions
                        'recommended_reduction': reduction_needed / len(sector_positions[:3])
                    })
            
            elif alert.alert_type == "asset_exposure":
                asset = alert.affected_entities[0]
                current_exp = alert.current_value
                reduction_needed = current_exp - self.limits.max_single_asset_exposure
                
                # Find positions in this asset
                asset_positions = [
                    (model_id, alloc) for model_id, alloc in self.portfolio_allocations.items()
                    if alloc.get('asset') == asset
                ]
                
                if asset_positions:
                    actions.append({
                        'type': 'reduce_asset_exposure',
                        'priority': 'critical' if alert.severity == RiskLevel.CRITICAL else 'high',
                        'description': f"Reduce {asset} exposure by {reduction_needed:.3f}",
                        'target_positions': asset_positions,
                        'recommended_reduction': reduction_needed / len(asset_positions)
                    })
            
            elif alert.alert_type == "diversification":
                # Find underrepresented sectors
                sector_exp = self.current_metrics.sector_exposures
                if sector_exp:
                    min_exp = min(sector_exp.values())
                    target_sectors = [s for s, exp in sector_exp.items() if exp < min_exp + 0.05]
                    
                    actions.append({
                        'type': 'increase_diversification',
                        'priority': 'medium',
                        'description': "Add positions in underrepresented sectors",
                        'target_sectors': target_sectors[:3],
                        'recommended_addition': 0.05  # 5% per new position
                    })
        
        return actions
    
    def get_risk_summary(self) -> Dict[str, Any]:
        """Get comprehensive risk summary"""
        return {
            'concentration_metrics': {
                'sector_exposures': dict(self.current_metrics.sector_exposures),
                'asset_exposures': dict(self.current_metrics.asset_exposures),
                'strategy_exposures': dict(self.current_metrics.strategy_exposures),
                'horizon_exposures': dict(self.current_metrics.horizon_exposures),
                'diversification_score': self.current_metrics.diversification_score,
                'total_positions': self.current_metrics.total_positions,
                'risk_level': self.current_metrics.concentration_risk_level.value
            },
            'active_alerts': [
                {
                    'type': alert.alert_type,
                    'severity': alert.severity.value,
                    'description': alert.description,
                    'current_value': alert.current_value,
                    'limit_value': alert.limit_value,
                    'affected_entities': alert.affected_entities,
                    'recommended_action': alert.recommended_action
                }
                for alert in self.risk_alerts
            ],
            'rebalancing_suggestions': self.suggest_rebalancing_actions(),
            'risk_limits': {
                'max_sector_exposure': self.limits.max_sector_exposure,
                'max_single_asset_exposure': self.limits.max_single_asset_exposure,
                'max_strategy_exposure': self.limits.max_strategy_exposure,
                'max_horizon_exposure': self.limits.max_horizon_exposure,
                'min_diversification_score': self.limits.min_diversification_score,
                'max_total_positions': self.limits.max_total_positions
            }
        }
    
    def adjust_limits_for_regime(self, regime: str) -> None:
        """Adjust concentration limits based on market regime"""
        if regime == "crisis":
            # Tighten limits in crisis
            self.limits.max_sector_exposure *= 0.8
            self.limits.max_single_asset_exposure *= 0.7
            self.limits.max_strategy_exposure *= 0.8
            self.limits.min_diversification_score += 0.1
        elif regime == "high_volatility":
            # Moderate tightening
            self.limits.max_sector_exposure *= 0.9
            self.limits.max_single_asset_exposure *= 0.85
            self.limits.min_diversification_score += 0.05
        elif regime == "low_volatility":
            # Can relax slightly
            self.limits.max_sector_exposure *= 1.1
            self.limits.max_single_asset_exposure *= 1.1
            self.limits.min_diversification_score -= 0.05
