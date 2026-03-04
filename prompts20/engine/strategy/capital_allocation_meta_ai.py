"""
Capital Allocation Meta-AI System

Design goals:
- Dynamically allocate capital across strategies
- Optimize return vs drawdown vs correlation
- Provide explainable capital shift diagnostics
- Safe degradation with confidence gating
- Cross-strategy capital intelligence

Inputs:
- Strategy returns, drawdowns, correlations
- Regime compatibility scores
- Performance metrics
- Risk constraints

Outputs:
- Optimal capital weights per strategy
- Allocation explanations
- Confidence scores
- Risk metrics
"""

import os
import json
import time
import math
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from collections import defaultdict
from datetime import datetime, timedelta
from scipy.optimize import minimize
import logging

from engine.storage import connect
from engine.strategy.capital_allocation_engine import CapitalAllocationEngine, AllocationTarget, CapitalConstraints
from engine.strategy.correlation_risk_analyzer import CorrelationRiskAnalyzer
from engine.regime_detection_system import RegimeDetector

logger = logging.getLogger(__name__)


@dataclass
class StrategyMetrics:
    """Comprehensive metrics for a single strategy"""
    name: str
    returns_1d: float
    returns_5d: float
    returns_30d: float
    volatility: float
    sharpe_ratio: float
    max_drawdown: float
    current_drawdown: float
    win_rate: float
    correlation_with_portfolio: float
    regime_compatibility: Dict[str, float]  # regime -> compatibility score
    performance_score: float = 1.0
    confidence_score: float = 0.5
    last_update: datetime = field(default_factory=datetime.now)


@dataclass 
class AllocationInputs:
    """All inputs required for capital allocation decision"""
    strategy_metrics: Dict[str, StrategyMetrics]
    correlation_matrix: Dict[str, Dict[str, float]]
    current_weights: Dict[str, float]
    total_capital: float
    regime: str
    market_volatility: float
    risk_tolerance: str = "medium"
    allocation_constraints: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AllocationDecision:
    """Meta-AI allocation decision with full explanation"""
    strategy: str
    old_weight: float
    new_weight: float
    weight_change: float
    expected_return: float
    risk_contribution: float
    reasoning: List[str]
    confidence: float
    regime_factor: float
    performance_factor: float
    correlation_penalty: float
    risk_adjustment: float


@dataclass
class OptimizationObjective:
    """Multi-objective optimization parameters"""
    return_weight: float = 0.4
    risk_weight: float = 0.3  
    correlation_weight: float = 0.2
    stability_weight: float = 0.1
    
    def validate(self) -> bool:
        return abs(sum([self.return_weight, self.risk_weight, 
                       self.correlation_weight, self.stability_weight]) - 1.0) < 0.001


class CapitalAllocationMetaAI:
    """
    Capital Allocation Meta-AI that dynamically allocates capital across strategies
    using multi-objective optimization and explainable AI.
    """
    
    def __init__(self, constraints: Optional[CapitalConstraints] = None):
        self.constraints = constraints or CapitalConstraints()
        self.regime_detector = RegimeDetector()
        self.correlation_analyzer = CorrelationRiskAnalyzer()
        
        # Optimization parameters
        self.objective = OptimizationObjective()
        self.min_allocation = 0.05  # 5% minimum per strategy
        self.max_allocation = 0.4   # 40% maximum per strategy
        self.rebalance_threshold = 0.1  # 10% change threshold
        
        # Confidence gating
        self.min_confidence_threshold = 0.6
        self.max_allocation_per_confidence = 0.3
        
        # Safety constraints
        self.max_single_strategy_drawdown = 0.15  # 15%
        self.max_portfolio_correlation = 0.7
        self.min_strategy_count = 2
        
        # History for diagnostics
        self.allocation_history: List[Dict] = []
        self.performance_tracking: Dict[str, List[float]] = defaultdict(list)
        
        logger.info("Initialized CapitalAllocationMetaAI")
    
    def collect_strategy_metrics(self, strategies: List[str]) -> Dict[str, StrategyMetrics]:
        """Collect comprehensive metrics for all strategies"""
        con = connect()
        metrics = {}
        
        try:
            for strategy in strategies:
                # Get recent performance data
                perf_data = self._get_strategy_performance(con, strategy)
                
                # Calculate returns at different horizons
                returns_1d, returns_5d, returns_30d = self._calculate_returns(con, strategy)
                
                # Get risk metrics
                volatility, sharpe, max_dd, current_dd = self._calculate_risk_metrics(con, strategy)
                
                # Win rate
                win_rate = self._calculate_win_rate(con, strategy)
                
                # Correlation with portfolio
                portfolio_corr = self._calculate_portfolio_correlation(con, strategy)
                
                # Regime compatibility
                regime_compat = self._calculate_regime_compatibility(strategy)
                
                # Performance and confidence scores
                perf_score = self._calculate_performance_score(perf_data)
                confidence = self._calculate_confidence_score(perf_data, volatility)
                
                metrics[strategy] = StrategyMetrics(
                    name=strategy,
                    returns_1d=returns_1d,
                    returns_5d=returns_5d, 
                    returns_30d=returns_30d,
                    volatility=volatility,
                    sharpe_ratio=sharpe,
                    max_drawdown=max_dd,
                    current_drawdown=current_dd,
                    win_rate=win_rate,
                    correlation_with_portfolio=portfolio_corr,
                    regime_compatibility=regime_compat,
                    performance_score=perf_score,
                    confidence_score=confidence
                )
                
        finally:
            con.close()
            
        return metrics
    
    def calculate_correlation_matrix(self, strategies: List[str]) -> Dict[str, Dict[str, float]]:
        """Calculate strategy correlation matrix using historical returns"""
        con = connect()
        correlations = {}
        
        try:
            # Get returns data for all strategies
            returns_data = {}
            for strategy in strategies:
                query = """
                SELECT date, daily_return 
                FROM strategy_daily_returns 
                WHERE strategy_name = ? 
                AND date >= date('now', '-90 days')
                ORDER BY date
                """
                rows = con.execute(query, (strategy,)).fetchall()
                if rows:
                    dates, returns = zip(*rows)
                    returns_data[strategy] = dict(zip(dates, returns))
            
            # Calculate correlations
            for i, strategy1 in enumerate(strategies):
                correlations[strategy1] = {}
                for j, strategy2 in enumerate(strategies):
                    if i == j:
                        correlations[strategy1][strategy2] = 1.0
                    else:
                        corr = self._calculate_correlation(
                            returns_data.get(strategy1, {}),
                            returns_data.get(strategy2, {})
                        )
                        correlations[strategy1][strategy2] = corr
                        
        finally:
            con.close()
            
        return correlations
    
    def optimize_allocation(self, inputs: AllocationInputs) -> Tuple[Dict[str, float], List[AllocationDecision]]:
        """
        Optimize capital allocation using multi-objective optimization
        
        Returns:
            optimal_weights: Dict[strategy] = weight
            decisions: List of allocation decisions with explanations
        """
        strategies = list(inputs.strategy_metrics.keys())
        n_strategies = len(strategies)
        
        if n_strategies < self.min_strategy_count:
            raise ValueError(f"Need at least {self.min_strategy_count} strategies for allocation")
        
        # Initial weights (equal weight or current weights)
        initial_weights = np.array([inputs.current_weights.get(s, 1.0/n_strategies) for s in strategies])
        
        # Optimization constraints
        constraints = [
            {'type': 'eq', 'fun': lambda w: np.sum(w) - 1.0},  # Weights sum to 1
        ]
        
        # Bounds for each strategy
        bounds = []
        for strategy in strategies:
            min_w = self.min_allocation
            max_w = self.max_allocation
            
            # Adjust bounds based on confidence
            confidence = inputs.strategy_metrics[strategy].confidence_score
            if confidence < self.min_confidence_threshold:
                max_w = min(max_w, self.max_allocation_per_confidence)
            
            # Adjust based on drawdown
            current_dd = inputs.strategy_metrics[strategy].current_drawdown
            if current_dd > self.max_single_strategy_drawdown:
                max_w = min(max_w, 0.1)  # Severely limit high drawdown strategies
            
            bounds.append((min_w, max_w))
        
        # Optimize
        result = minimize(
            self._objective_function,
            initial_weights,
            args=(inputs, strategies),
            method='SLSQP',
            bounds=bounds,
            constraints=constraints,
            options={'maxiter': 1000, 'ftol': 1e-6}
        )
        
        if not result.success:
            logger.warning(f"Optimization failed: {result.message}")
            # Fall back to current weights
            optimal_weights = inputs.current_weights
        else:
            optimal_weights = dict(zip(strategies, result.x))
        
        # Generate allocation decisions with explanations
        decisions = self._generate_allocation_decisions(inputs, optimal_weights)
        
        return optimal_weights, decisions
    
    def _objective_function(self, weights: np.ndarray, inputs: AllocationInputs, strategies: List[str]) -> float:
        """Multi-objective function for optimization"""
        weights_dict = dict(zip(strategies, weights))
        
        # 1. Expected return component
        expected_return = sum(
            weights_dict[s] * inputs.strategy_metrics[s].returns_30d 
            for s in strategies
        )
        
        # 2. Risk component (negative for minimization)
        portfolio_risk = 0.0
        for i, s1 in enumerate(strategies):
            for j, s2 in enumerate(strategies):
                portfolio_risk += (
                    weights[i] * weights[j] * 
                    inputs.strategy_metrics[s1].volatility * 
                    inputs.strategy_metrics[s2].volatility *
                    inputs.correlation_matrix.get(s1, {}).get(s2, 0.0)
                )
        
        # 3. Correlation penalty
        avg_correlation = np.mean([
            inputs.correlation_matrix.get(s1, {}).get(s2, 0.0)
            for i, s1 in enumerate(strategies)
            for j, s2 in enumerate(strategies)
            if i != j
        ])
        correlation_penalty = avg_correlation * 0.5
        
        # 4. Stability penalty (deviation from current weights)
        stability_penalty = sum(
            abs(weights_dict[s] - inputs.current_weights.get(s, 0.0))
            for s in strategies
        ) * 0.1
        
        # Combine objectives (weighted sum)
        objective = (
            -self.objective.return_weight * expected_return +
            self.objective.risk_weight * portfolio_risk +
            self.objective.correlation_weight * correlation_penalty +
            self.objective.stability_weight * stability_penalty
        )
        
        return objective
    
    def _generate_allocation_decisions(self, inputs: AllocationInputs, optimal_weights: Dict[str, float]) -> List[AllocationDecision]:
        """Generate detailed allocation decisions with explanations"""
        decisions = []
        
        for strategy in inputs.strategy_metrics.keys():
            old_weight = inputs.current_weights.get(strategy, 0.0)
            new_weight = optimal_weights[strategy]
            weight_change = new_weight - old_weight
            
            # Skip insignificant changes
            if abs(weight_change) < self.rebalance_threshold:
                continue
            
            metrics = inputs.strategy_metrics[strategy]
            
            # Calculate expected return contribution
            expected_return = new_weight * metrics.returns_30d
            
            # Calculate risk contribution
            risk_contribution = 0.0
            for other_strategy in inputs.strategy_metrics.keys():
                corr = inputs.correlation_matrix.get(strategy, {}).get(other_strategy, 0.0)
                risk_contribution += (
                    new_weight * optimal_weights[other_strategy] *
                    metrics.volatility * inputs.strategy_metrics[other_strategy].volatility * corr
                )
            
            # Generate reasoning
            reasoning = []
            
            # Performance reasoning
            if metrics.performance_score > 1.2:
                reasoning.append(f"Strong performance (score: {metrics.performance_score:.2f})")
            elif metrics.performance_score < 0.8:
                reasoning.append(f"Weak performance (score: {metrics.performance_score:.2f})")
            
            # Risk reasoning
            if metrics.current_drawdown > 0.1:
                reasoning.append(f"High drawdown ({metrics.current_drawdown:.1%}) reduces allocation")
            elif metrics.volatility < 0.15:
                reasoning.append(f"Low volatility ({metrics.volatility:.1%}) supports higher allocation")
            
            # Correlation reasoning
            if metrics.correlation_with_portfolio > 0.7:
                reasoning.append(f"High correlation ({metrics.correlation_with_portfolio:.2f}) limits diversification benefit")
            elif metrics.correlation_with_portfolio < 0.3:
                reasoning.append(f"Low correlation ({metrics.correlation_with_portfolio:.2f}) provides diversification")
            
            # Regime reasoning
            current_regime = inputs.regime
            regime_compat = metrics.regime_compatibility.get(current_regime, 0.5)
            if regime_compat > 0.7:
                reasoning.append(f"Strong regime compatibility for {current_regime}")
            elif regime_compat < 0.3:
                reasoning.append(f"Poor regime compatibility for {current_regime}")
            
            # Confidence reasoning
            if metrics.confidence_score < self.min_confidence_threshold:
                reasoning.append(f"Low confidence score ({metrics.confidence_score:.2f}) limits allocation")
            
            # Calculate factors
            regime_factor = regime_compat
            performance_factor = min(2.0, max(0.1, metrics.performance_score))
            correlation_penalty = max(0.0, metrics.correlation_with_portfolio - 0.5) * 2.0
            risk_adjustment = max(0.1, 1.0 - metrics.current_drawdown * 2.0)
            
            decision = AllocationDecision(
                strategy=strategy,
                old_weight=old_weight,
                new_weight=new_weight,
                weight_change=weight_change,
                expected_return=expected_return,
                risk_contribution=risk_contribution,
                reasoning=reasoning,
                confidence=metrics.confidence_score,
                regime_factor=regime_factor,
                performance_factor=performance_factor,
                correlation_penalty=correlation_penalty,
                risk_adjustment=risk_adjustment
            )
            
            decisions.append(decision)
        
        return decisions
    
    def get_allocation_diagnostics(self, decisions: List[AllocationDecision]) -> Dict[str, Any]:
        """Generate comprehensive diagnostics for allocation changes"""
        if not decisions:
            return {"status": "no_changes", "message": "No significant allocation changes"}
        
        # Aggregate statistics
        total_change = sum(abs(d.weight_change) for d in decisions)
        increasing = [d for d in decisions if d.weight_change > 0]
        decreasing = [d for d in decisions if d.weight_change < 0]
        
        # Risk analysis
        avg_correlation = np.mean([d.correlation_penalty for d in decisions])
        avg_confidence = np.mean([d.confidence for d in decisions])
        
        # Performance expectations
        expected_return = sum(d.expected_return for d in decisions)
        total_risk = sum(d.risk_contribution for d in decisions)
        
        # Primary drivers
        all_reasons = []
        for d in decisions:
            all_reasons.extend(d.reasoning)
        
        reason_counts = {}
        for reason in all_reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        
        top_reasons = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        
        diagnostics = {
            "summary": {
                "total_allocation_change": total_change,
                "strategies_increasing": len(increasing),
                "strategies_decreasing": len(decreasing),
                "expected_portfolio_return": expected_return,
                "portfolio_risk_contribution": total_risk,
                "avg_confidence": avg_confidence,
                "avg_correlation_penalty": avg_correlation
            },
            "top_drivers": top_reasons,
            "individual_changes": [
                {
                    "strategy": d.strategy,
                    "change": f"{d.weight_change:+.2%}",
                    "new_weight": f"{d.new_weight:.2%}",
                    "confidence": d.confidence,
                    "primary_reason": d.reasoning[0] if d.reasoning else "No specific reason"
                }
                for d in decisions
            ],
            "risk_metrics": {
                "diversification_score": max(0.0, 1.0 - avg_correlation),
                "confidence_distribution": {
                    "high": len([d for d in decisions if d.confidence > 0.8]),
                    "medium": len([d for d in decisions if 0.5 <= d.confidence <= 0.8]),
                    "low": len([d for d in decisions if d.confidence < 0.5])
                }
            },
            "timestamp": datetime.now().isoformat()
        }
        
        return diagnostics
    
    # Helper methods (implementations would go here)
    def _get_strategy_performance(self, con, strategy: str) -> Dict[str, Any]:
        """Get recent performance data for strategy"""
        query = """
        SELECT sharpe_ratio, total_return, max_drawdown, win_rate, ts_ms
        FROM strategy_performance 
        WHERE strategy_name = ?
        ORDER BY ts_ms DESC 
        LIMIT 1
        """
        row = con.execute(query, (strategy,)).fetchone()
        if row:
            return {
                "sharpe_ratio": row[0] or 0.0,
                "total_return": row[1] or 0.0,
                "max_drawdown": row[2] or 0.0,
                "win_rate": row[3] or 0.0,
                "timestamp": row[4] or 0
            }
        return {}
    
    def _calculate_returns(self, con, strategy: str) -> Tuple[float, float, float]:
        """Calculate 1d, 5d, 30d returns"""
        # Implementation would query strategy returns table
        return 0.001, 0.005, 0.02  # Placeholder
    
    def _calculate_risk_metrics(self, con, strategy: str) -> Tuple[float, float, float, float]:
        """Calculate volatility, sharpe, max drawdown, current drawdown"""
        # Implementation would calculate from historical returns
        return 0.15, 1.2, 0.08, 0.03  # Placeholder
    
    def _calculate_win_rate(self, con, strategy: str) -> float:
        """Calculate strategy win rate"""
        # Implementation would calculate from trade history
        return 0.62  # Placeholder
    
    def _calculate_portfolio_correlation(self, con, strategy: str) -> float:
        """Calculate correlation with overall portfolio"""
        # Implementation would calculate correlation with portfolio returns
        return 0.45  # Placeholder
    
    def _calculate_regime_compatibility(self, strategy: str) -> Dict[str, float]:
        """Calculate strategy compatibility with different market regimes"""
        # Implementation would analyze performance across regimes
        return {
            "bull_market": 0.8,
            "bear_market": 0.4,
            "sideways": 0.6,
            "high_volatility": 0.3,
            "low_volatility": 0.9
        }
    
    def _calculate_performance_score(self, perf_data: Dict[str, Any]) -> float:
        """Calculate composite performance score"""
        if not perf_data:
            return 1.0
        
        sharpe = perf_data.get("sharpe_ratio", 0.0)
        return_rate = perf_data.get("total_return", 0.0)
        max_dd = perf_data.get("max_drawdown", 0.0)
        win_rate = perf_data.get("win_rate", 0.0)
        
        # Composite score (higher is better)
        score = (
            min(2.0, max(0.1, sharpe)) * 0.4 +
            min(2.0, max(0.1, 1.0 + return_rate)) * 0.3 +
            min(2.0, max(0.1, 1.0 - max_dd)) * 0.2 +
            min(2.0, max(0.1, win_rate)) * 0.1
        )
        
        return score
    
    def _calculate_confidence_score(self, perf_data: Dict[str, Any], volatility: float) -> float:
        """Calculate confidence score based on data quality and stability"""
        base_confidence = 0.7
        
        # Adjust based on data recency
        timestamp = perf_data.get("timestamp", 0)
        if timestamp:
            age_hours = (time.time() * 1000 - timestamp) / (1000 * 3600)
            if age_hours > 168:  # 1 week
                base_confidence -= 0.2
            elif age_hours > 24:  # 1 day
                base_confidence -= 0.1
        
        # Adjust based on volatility
        if volatility > 0.25:
            base_confidence -= 0.15
        elif volatility < 0.1:
            base_confidence += 0.1
        
        return max(0.1, min(1.0, base_confidence))
    
    def _calculate_correlation(self, returns1: Dict[str, float], returns2: Dict[str, float]) -> float:
        """Calculate correlation between two return series"""
        if not returns1 or not returns2:
            return 0.0
        
        # Find common dates
        common_dates = set(returns1.keys()) & set(returns2.keys())
        if len(common_dates) < 10:
            return 0.0
        
        # Extract returns for common dates
        r1 = np.array([returns1[date] for date in sorted(common_dates)])
        r2 = np.array([returns2[date] for date in sorted(common_dates)])
        
        # Calculate correlation
        if len(r1) < 2 or np.std(r1) == 0 or np.std(r2) == 0:
            return 0.0
        
        correlation = np.corrcoef(r1, r2)[0, 1]
        return correlation if not np.isnan(correlation) else 0.0


def run_meta_allocation(strategies: List[str], current_weights: Dict[str, float], 
                      total_capital: float = 500000.0) -> Dict[str, Any]:
    """
    Main entry point for running capital allocation meta-AI
    
    Returns:
        {
            "optimal_weights": {...},
            "decisions": [...],
            "diagnostics": {...},
            "success": True/False,
            "message": "..."
        }
    """
    try:
        meta_ai = CapitalAllocationMetaAI()
        
        # Collect inputs
        strategy_metrics = meta_ai.collect_strategy_metrics(strategies)
        correlation_matrix = meta_ai.calculate_correlation_matrix(strategies)
        
        # Get current regime
        current_regime = meta_ai.regime_detector.get_current_regime()
        
        # Create inputs object
        inputs = AllocationInputs(
            strategy_metrics=strategy_metrics,
            correlation_matrix=correlation_matrix,
            current_weights=current_weights,
            total_capital=total_capital,
            regime=current_regime,
            market_volatility=0.15  # Would get from market data
        )
        
        # Optimize allocation
        optimal_weights, decisions = meta_ai.optimize_allocation(inputs)
        
        # Generate diagnostics
        diagnostics = meta_ai.get_allocation_diagnostics(decisions)
        
        return {
            "optimal_weights": optimal_weights,
            "decisions": [vars(d) for d in decisions],
            "diagnostics": diagnostics,
            "success": True,
            "message": f"Allocation optimized for {len(decisions)} strategies"
        }
        
    except Exception as e:
        logger.error(f"Meta allocation failed: {e}")
        return {
            "optimal_weights": current_weights,
            "decisions": [],
            "diagnostics": {"error": str(e)},
            "success": False,
            "message": f"Allocation failed: {str(e)}"
        }
