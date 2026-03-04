"""
Budget Reallocator

Advanced system for reallocating budget from unproductive research paths to promising areas.
Implements dynamic reallocation based on performance trends, opportunity cost analysis,
and portfolio optimization techniques.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional, Set
from dataclasses import dataclass, field
from enum import Enum
import logging
from datetime import datetime, timedelta
from scipy.optimize import minimize
from research_budget_allocator import ResearchAsset, ResearchStatus, AllocationDecision
from stop_criteria_manager import StopCriteriaManager, StopDecision

logger = logging.getLogger(__name__)

class ReallocationTrigger(Enum):
    PERFORMANCE_DECLINE = "performance_decline"
    ASSET_STOPPED = "asset_stopped"
    OPPORTUNITY_DETECTED = "opportunity_detected"
    SCHEDULED_REALLOCATION = "scheduled_reallocation"
    BUDGET_SURPLUS = "budget_surplus"
    STRATEGIC_SHIFT = "strategic_shift"

class ReallocationStrategy(Enum):
    PROPORTIONAL_PERFORMANCE = "proportional_performance"
    OPPORTUNITY_COST = "opportunity_cost"
    PORTFOLIO_OPTIMIZATION = "portfolio_optimization"
    CONSERVATIVE_REBALANCE = "conservative_rebalance"
    AGGRESSIVE_GROWTH = "aggressive_growth"

@dataclass
class ReallocationEvent:
    """Represents a budget reallocation event"""
    trigger: ReallocationTrigger
    strategy: ReallocationStrategy
    amount_reallocated: float
    source_assets: List[str]  # Assets losing budget
    target_assets: List[str]  # Assets gaining budget
    reasoning: str
    confidence: float
    timestamp: datetime = field(default_factory=datetime.now)
    
@dataclass
class ReallocationDecision:
    """Detailed decision for a single asset's reallocation"""
    asset_id: str
    current_budget: float
    recommended_budget: float
    change_amount: float
    change_percentage: float
    reasoning: str
    risk_adjustment: float
    opportunity_score: float

class BudgetReallocator:
    """Advanced budget reallocation system"""
    
    def __init__(self, reallocation_strategy: ReallocationStrategy = ReallocationStrategy.PORTFOLIO_OPTIMIZATION,
                 min_reallocation_amount: float = 1000, max_reallocation_frequency: int = 7):
        self.reallocation_strategy = reallocation_strategy
        self.min_reallocation_amount = min_reallocation_amount
        self.max_reallocation_frequency = max_reallocation_frequency  # Days between reallocations
        
        self.reallocation_history: List[ReallocationEvent] = []
        self.asset_reallocation_counts: Dict[str, int] = {}
        self.last_reallocation_time: Dict[str, datetime] = {}
        
        # Reallocation parameters
        self.max_budget_change_percentage = 0.3  # Max 30% change per reallocation
        self.min_budget_reserve = 0.1  # Keep 10% as reserve
        self.performance_window = 10  # Look back window for performance analysis
        
        # Opportunity detection parameters
        self.opportunity_threshold = 0.2  # 20% improvement triggers opportunity
        self.decline_threshold = -0.15  # -15% decline triggers reallocation
        
        logger.info(f"Initialized BudgetReallocator with {reallocation_strategy.value} strategy")
    
    def should_reallocate(self, assets: Dict[str, ResearchAsset], 
                         stop_decisions: Dict[str, List[StopDecision]]) -> Tuple[bool, ReallocationTrigger, str]:
        """
        Determine if budget reallocation should be triggered
        Returns: (should_reallocate, trigger, reasoning)
        """
        triggers = []
        
        # Check for stopped assets (highest priority)
        stopped_assets = [asset_id for asset_id, decisions in stop_decisions.items() 
                        if any(d.should_stop for d in decisions)]
        
        if stopped_assets:
            total_stopped_budget = sum(assets[asset_id].current_budget for asset_id in stopped_assets)
            if total_stopped_budget >= self.min_reallocation_amount:
                triggers.append((ReallocationTrigger.ASSET_STOPPED, 
                              f"Assets {stopped_assets} stopped with ${total_stopped_budget:,.2f} to reallocate"))
        
        # Check for performance declines
        declining_assets = self._identify_declining_assets(assets)
        if declining_assets:
            total_declining_budget = sum(assets[asset_id].current_budget for asset_id in declining_assets)
            if total_declining_budget >= self.min_reallocation_amount:
                triggers.append((ReallocationTrigger.PERFORMANCE_DECLINE,
                              f"Assets {declining_assets} showing performance decline"))
        
        # Check for opportunities
        opportunity_assets = self._identify_opportunities(assets)
        if opportunity_assets:
            triggers.append((ReallocationTrigger.OPPORTUNITY_DETECTED,
                          f"High-potential opportunities detected in {opportunity_assets}"))
        
        # Check for scheduled reallocation
        if self._should_schedule_reallocation():
            triggers.append((ReallocationTrigger.SCHEDULED_REALLOCATION,
                          "Scheduled periodic reallocation"))
        
        # Check for budget surplus
        total_budget = sum(asset.current_budget for asset in assets.values())
        total_allocated = sum(asset.initial_budget for asset in assets.values())
        if total_budget > total_allocated * (1 + self.min_budget_reserve):
            triggers.append((ReallocationTrigger.BUDGET_SURPLUS,
                          f"Budget surplus detected: ${total_budget - total_allocated:,.2f}"))
        
        # Return highest priority trigger
        if triggers:
            # Priority order: ASSET_STOPPED > OPPORTUNITY_DETECTED > PERFORMANCE_DECLINE > others
            priority_order = {
                ReallocationTrigger.ASSET_STOPPED: 5,
                ReallocationTrigger.OPPORTUNITY_DETECTED: 4,
                ReallocationTrigger.PERFORMANCE_DECLINE: 3,
                ReallocationTrigger.BUDGET_SURPLUS: 2,
                ReallocationTrigger.SCHEDULED_REALLOCATION: 1,
                ReallocationTrigger.STRATEGIC_SHIFT: 0
            }
            
            triggers.sort(key=lambda x: priority_order.get(x[0], 0), reverse=True)
            return True, triggers[0][0], triggers[0][1]
        
        return False, ReallocationTrigger.SCHEDULED_REALLOCATION, "No trigger conditions met"
    
    def _identify_declining_assets(self, assets: Dict[str, ResearchAsset]) -> List[str]:
        """Identify assets with declining performance"""
        declining = []
        
        for asset_id, asset in assets.items():
            if asset.status != ResearchStatus.ACTIVE or len(asset.recent_performance) < self.performance_window:
                continue
            
            recent_perf = asset.recent_performance[-self.performance_window:]
            
            # Calculate trend
            x = np.arange(len(recent_perf))
            slope, _, _, _, _ = np.polyfit(x, recent_perf, 1, full=True)
            
            # Check if decline is significant
            if slope < self.decline_threshold / self.performance_window:
                declining.append(asset_id)
        
        return declining
    
    def _identify_opportunities(self, assets: Dict[str, ResearchAsset]) -> List[str]:
        """Identify assets with high opportunity potential"""
        opportunities = []
        
        for asset_id, asset in assets.items():
            if asset.status != ResearchStatus.ACTIVE:
                continue
            
            # High expected payoff with low budget
            if (asset.expected_payoff > 0.7 and 
                asset.current_budget < asset.initial_budget * 0.5 and
                asset.confidence_score > 0.5):
                opportunities.append(asset_id)
            
            # Recent significant improvement
            elif (len(asset.recent_performance) >= 3 and
                  asset.recent_performance[-1] > asset.recent_performance[-3] * (1 + self.opportunity_threshold)):
                opportunities.append(asset_id)
        
        return opportunities
    
    def _should_schedule_reallocation(self) -> bool:
        """Check if scheduled reallocation is due"""
        if not self.reallocation_history:
            return True
        
        last_reallocation = max(event.timestamp for event in self.reallocation_history)
        days_since_last = (datetime.now() - last_reallocation).days
        
        return days_since_last >= self.max_reallocation_frequency
    
    def calculate_reallocation(self, assets: Dict[str, ResearchAsset], 
                             trigger: ReallocationTrigger, 
                             total_available: float) -> List[ReallocationDecision]:
        """
        Calculate optimal reallocation decisions
        Returns: List of reallocation decisions for each asset
        """
        active_assets = {k: v for k, v in assets.items() if v.status == ResearchStatus.ACTIVE}
        
        if not active_assets:
            return []
        
        if self.reallocation_strategy == ReallocationStrategy.PROPORTIONAL_PERFORMANCE:
            return self._proportional_performance_reallocation(active_assets, total_available)
        elif self.reallocation_strategy == ReallocationStrategy.OPPORTUNITY_COST:
            return self._opportunity_cost_reallocation(active_assets, total_available)
        elif self.reallocation_strategy == ReallocationStrategy.PORTFOLIO_OPTIMIZATION:
            return self._portfolio_optimization_reallocation(active_assets, total_available)
        elif self.reallocation_strategy == ReallocationStrategy.CONSERVATIVE_REBALANCE:
            return self._conservative_rebalance(active_assets, total_available)
        elif self.reallocation_strategy == ReallocationStrategy.AGGRESSIVE_GROWTH:
            return self._aggressive_growth_reallocation(active_assets, total_available)
        else:
            return self._proportional_performance_reallocation(active_assets, total_available)
    
    def _proportional_performance_reallocation(self, assets: Dict[str, ResearchAsset], 
                                             total_available: float) -> List[ReallocationDecision]:
        """Reallocate based on proportional performance scores"""
        decisions = []
        
        # Calculate performance scores
        scores = {}
        for asset_id, asset in assets.items():
            # Combine expected payoff, confidence, and exploration bonus
            score = (asset.expected_payoff * asset.confidence_score + 
                    asset.exploration_score * (1 - asset.confidence_score))
            scores[asset_id] = score
        
        # Normalize scores
        total_score = sum(scores.values())
        if total_score == 0:
            # Equal allocation if all scores are zero
            equal_share = total_available / len(assets)
            for asset_id, asset in assets.items():
                decisions.append(ReallocationDecision(
                    asset_id=asset_id,
                    current_budget=asset.current_budget,
                    recommended_budget=asset.current_budget + equal_share,
                    change_amount=equal_share,
                    change_percentage=equal_share / (asset.current_budget + 1e-6),
                    reasoning="Equal allocation (all scores zero)",
                    risk_adjustment=0.0,
                    opportunity_score=0.0
                ))
        else:
            # Proportional allocation based on scores
            for asset_id, asset in assets.items():
                score = scores[asset_id]
                allocation = total_available * (score / total_score)
                
                # Apply constraints
                max_change = asset.current_budget * self.max_budget_change_percentage
                allocation = min(allocation, max_change)
                allocation = max(-max_change, allocation)  # Can't reduce too much either
                
                new_budget = asset.current_budget + allocation
                
                decisions.append(ReallocationDecision(
                    asset_id=asset_id,
                    current_budget=asset.current_budget,
                    recommended_budget=new_budget,
                    change_amount=allocation,
                    change_percentage=allocation / (asset.current_budget + 1e-6),
                    reasoning=f"Proportional to performance score {score:.3f}",
                    risk_adjustment=0.0,
                    opportunity_score=score
                ))
        
        return decisions
    
    def _opportunity_cost_reallocation(self, assets: Dict[str, ResearchAsset], 
                                     total_available: float) -> List[ReallocationDecision]:
        """Reallocate based on opportunity cost analysis"""
        decisions = []
        
        # Calculate opportunity costs
        opportunity_scores = {}
        for asset_id, asset in assets.items():
            # Opportunity cost = potential gain - current performance
            potential_gain = asset.expected_payoff * asset.confidence_score
            current_return = asset.expected_payoff if asset.total_spent > 0 else 0
            opportunity_cost = potential_gain - current_return
            
            # Adjust for risk (higher budget = higher opportunity cost for poor performers)
            risk_adjustment = self._calculate_risk_adjustment(asset)
            adjusted_score = opportunity_cost * (1 - risk_adjustment)
            
            opportunity_scores[asset_id] = adjusted_score
        
        # Sort by opportunity score (highest first)
        sorted_assets = sorted(opportunity_scores.items(), key=lambda x: x[1], reverse=True)
        
        # Allocate to high-opportunity assets first
        remaining_budget = total_available
        decisions_dict = {}
        
        for asset_id, score in sorted_assets:
            if remaining_budget <= 0:
                break
            
            asset = assets[asset_id]
            
            # Calculate allocation based on opportunity score
            if score > 0:
                # Positive opportunity: allocate more
                allocation = min(remaining_budget, asset.current_budget * 0.2)  # Up to 20% increase
            else:
                # Negative opportunity: reduce allocation
                allocation = max(-remaining_budget, -asset.current_budget * 0.1)  # Up to 10% decrease
            
            new_budget = asset.current_budget + allocation
            remaining_budget -= allocation
            
            decisions_dict[asset_id] = ReallocationDecision(
                asset_id=asset_id,
                current_budget=asset.current_budget,
                recommended_budget=new_budget,
                change_amount=allocation,
                change_percentage=allocation / (asset.current_budget + 1e-6),
                reasoning=f"Opportunity cost analysis: score {score:.3f}",
                risk_adjustment=self._calculate_risk_adjustment(asset),
                opportunity_score=score
            )
        
        # Add decisions for remaining assets (no change)
        for asset_id, asset in assets.items():
            if asset_id not in decisions_dict:
                decisions_dict[asset_id] = ReallocationDecision(
                    asset_id=asset_id,
                    current_budget=asset.current_budget,
                    recommended_budget=asset.current_budget,
                    change_amount=0.0,
                    change_percentage=0.0,
                    reasoning="No opportunity identified",
                    risk_adjustment=self._calculate_risk_adjustment(asset),
                    opportunity_score=opportunity_scores.get(asset_id, 0.0)
                )
        
        return list(decisions_dict.values())
    
    def _portfolio_optimization_reallocation(self, assets: Dict[str, ResearchAsset], 
                                           total_available: float) -> List[ReallocationDecision]:
        """Use portfolio optimization for reallocation"""
        decisions = []
        
        # Create expected returns and covariance matrix
        asset_ids = list(assets.keys())
        n_assets = len(asset_ids)
        
        if n_assets < 2:
            return self._proportional_performance_reallocation(assets, total_available)
        
        # Expected returns (based on performance)
        expected_returns = np.array([
            assets[asset_id].expected_payoff * assets[asset_id].confidence_score
            for asset_id in asset_ids
        ])
        
        # Risk (inverse of confidence score)
        risks = np.array([
            1 - assets[asset_id].confidence_score
            for asset_id in asset_ids
        ])
        
        # Covariance matrix (simplified - diagonal with risks)
        cov_matrix = np.diag(risks ** 2)
        
        # Portfolio optimization: maximize Sharpe ratio
        def objective(weights):
            portfolio_return = np.dot(weights, expected_returns)
            portfolio_risk = np.sqrt(np.dot(weights, np.dot(cov_matrix, weights)))
            return -(portfolio_return / (portfolio_risk + 1e-6))  # Negative for minimization
        
        # Constraints: weights sum to 1, no negative weights
        constraints = ({'type': 'eq', 'fun': lambda w: np.sum(w) - 1})
        bounds = tuple((0, 1) for _ in range(n_assets))
        
        # Initial guess (equal weights)
        x0 = np.array([1/n_assets] * n_assets)
        
        try:
            result = minimize(objective, x0, method='SLSQP', bounds=bounds, constraints=constraints)
            
            if result.success:
                optimal_weights = result.x
                
                # Calculate target budgets based on optimal weights
                total_current_budget = sum(asset.current_budget for asset in assets.values())
                target_budgets = total_current_budget * optimal_weights
                
                for i, asset_id in enumerate(asset_ids):
                    asset = assets[asset_id]
                    target_budget = target_budgets[i]
                    change = target_budget - asset.current_budget
                    
                    # Apply constraints
                    max_change = asset.current_budget * self.max_budget_change_percentage
                    change = np.clip(change, -max_change, max_change)
                    
                    final_budget = asset.current_budget + change
                    
                    decisions.append(ReallocationDecision(
                        asset_id=asset_id,
                        current_budget=asset.current_budget,
                        recommended_budget=final_budget,
                        change_amount=change,
                        change_percentage=change / (asset.current_budget + 1e-6),
                        reasoning=f"Portfolio optimization: weight {optimal_weights[i]:.3f}",
                        risk_adjustment=risks[i],
                        opportunity_score=expected_returns[i]
                    ))
            else:
                # Fallback to proportional performance
                return self._proportional_performance_reallocation(assets, total_available)
        
        except Exception as e:
            logger.error(f"Portfolio optimization failed: {e}")
            return self._proportional_performance_reallocation(assets, total_available)
        
        return decisions
    
    def _conservative_rebalance(self, assets: Dict[str, ResearchAsset], 
                              total_available: float) -> List[ReallocationDecision]:
        """Conservative rebalancing with minimal changes"""
        decisions = []
        
        # Calculate target allocation based on initial proportions
        total_initial = sum(asset.initial_budget for asset in assets.values())
        
        for asset_id, asset in assets.items():
            if total_initial > 0:
                target_proportion = asset.initial_budget / total_initial
                target_budget = sum(asset.current_budget for asset in assets.values()) * target_proportion
                
                # Conservative change: only 10% of the difference
                change = (target_budget - asset.current_budget) * 0.1
                
                # Apply minimum change threshold
                if abs(change) < self.min_reallocation_amount * 0.1:
                    change = 0
                
                new_budget = asset.current_budget + change
                
                decisions.append(ReallocationDecision(
                    asset_id=asset_id,
                    current_budget=asset.current_budget,
                    recommended_budget=new_budget,
                    change_amount=change,
                    change_percentage=change / (asset.current_budget + 1e-6),
                    reasoning=f"Conservative rebalancing to initial proportion {target_proportion:.3f}",
                    risk_adjustment=0.5,  # Medium risk adjustment
                    opportunity_score=asset.expected_payoff
                ))
        
        return decisions
    
    def _aggressive_growth_reallocation(self, assets: Dict[str, ResearchAsset], 
                                      total_available: float) -> List[ReallocationDecision]:
        """Aggressive growth strategy focusing on high performers"""
        decisions = []
        
        # Identify top performers
        performance_scores = {
            asset_id: asset.expected_payoff * asset.confidence_score
            for asset_id, asset in assets.items()
        }
        
        # Sort by performance
        sorted_assets = sorted(performance_scores.items(), key=lambda x: x[1], reverse=True)
        
        # Allocate 70% to top 30% performers, 30% to rest
        top_performers_count = max(1, len(sorted_assets) // 3)
        top_performers = [asset_id for asset_id, _ in sorted_assets[:top_performers_count]]
        
        total_top_budget = total_available * 0.7
        total_rest_budget = total_available * 0.3
        
        # Allocate to top performers
        if top_performers:
            top_score_sum = sum(performance_scores[aid] for aid in top_performers)
            
            for asset_id, asset in assets.items():
                if asset_id in top_performers:
                    proportion = performance_scores[asset_id] / top_score_sum
                    allocation = total_top_budget * proportion
                    
                    # Aggressive increase (up to 50%)
                    max_increase = asset.current_budget * 0.5
                    allocation = min(allocation, max_increase)
                    
                    new_budget = asset.current_budget + allocation
                    
                    decisions.append(ReallocationDecision(
                        asset_id=asset_id,
                        current_budget=asset.current_budget,
                        recommended_budget=new_budget,
                        change_amount=allocation,
                        change_percentage=allocation / (asset.current_budget + 1e-6),
                        reasoning=f"Aggressive growth: top performer (score {performance_scores[asset_id]:.3f})",
                        risk_adjustment=0.2,  # Low risk adjustment for top performers
                        opportunity_score=performance_scores[asset_id]
                    ))
                else:
                    # Small allocation to rest
                    allocation = total_rest_budget / (len(assets) - len(top_performers))
                    new_budget = asset.current_budget + allocation
                    
                    decisions.append(ReallocationDecision(
                        asset_id=asset_id,
                        current_budget=asset.current_budget,
                        recommended_budget=new_budget,
                        change_amount=allocation,
                        change_percentage=allocation / (asset.current_budget + 1e-6),
                        reasoning="Aggressive growth: secondary allocation",
                        risk_adjustment=0.8,  # High risk adjustment for others
                        opportunity_score=performance_scores[asset_id]
                    ))
        
        return decisions
    
    def _calculate_risk_adjustment(self, asset: ResearchAsset) -> float:
        """Calculate risk adjustment factor for an asset"""
        risk_factors = []
        
        # Budget utilization risk
        if asset.current_budget < asset.initial_budget * 0.1:
            risk_factors.append(0.3)
        
        # Performance volatility
        if len(asset.recent_performance) >= 3:
            volatility = np.std(asset.recent_performance) / (np.mean(asset.recent_performance) + 1e-6)
            risk_factors.append(min(volatility, 0.5))
        
        # Confidence risk (low confidence = higher risk)
        risk_factors.append(1 - asset.confidence_score)
        
        return min(sum(risk_factors), 0.9)
    
    def execute_reallocation(self, assets: Dict[str, ResearchAsset], 
                           decisions: List[ReallocationDecision]) -> ReallocationEvent:
        """Execute the reallocation decisions"""
        source_assets = []
        target_assets = []
        total_reallocated = 0
        
        for decision in decisions:
            asset = assets[decision.asset_id]
            
            # Update asset budget
            asset.current_budget = decision.recommended_budget
            
            if decision.change_amount > 0:
                target_assets.append(decision.asset_id)
                total_reallocated += decision.change_amount
            elif decision.change_amount < 0:
                source_assets.append(decision.asset_id)
            
            # Update reallocation tracking
            self.asset_reallocation_counts[decision.asset_id] = self.asset_reallocation_counts.get(decision.asset_id, 0) + 1
            self.last_reallocation_time[decision.asset_id] = datetime.now()
        
        # Create reallocation event
        event = ReallocationEvent(
            trigger=ReallocationTrigger.OPPORTUNITY_DETECTED,  # This should be passed as parameter
            strategy=self.reallocation_strategy,
            amount_reallocated=total_reallocated,
            source_assets=source_assets,
            target_assets=target_assets,
            reasoning=f"Reallocated ${total_reallocated:,.2f} from {len(source_assets)} sources to {len(target_assets)} targets",
            confidence=0.8  # This could be calculated based on decision consistency
        )
        
        self.reallocation_history.append(event)
        logger.info(f"Executed reallocation: ${total_reallocated:,.2f} across {len(decisions)} assets")
        
        return event
    
    def get_reallocation_summary(self) -> Dict:
        """Get summary of reallocation activity"""
        if not self.reallocation_history:
            return {"total_events": 0, "total_reallocated": 0.0}
        
        total_reallocated = sum(event.amount_reallocated for event in self.reallocation_history)
        
        # Asset reallocation frequency
        asset_frequency = {}
        for asset_id, count in self.asset_reallocation_counts.items():
            asset_frequency[asset_id] = count
        
        # Recent events
        recent_events = [
            {
                "timestamp": event.timestamp.isoformat(),
                "trigger": event.trigger.value,
                "strategy": event.strategy.value,
                "amount": event.amount_reallocated,
                "source_count": len(event.source_assets),
                "target_count": len(event.target_assets)
            }
            for event in self.reallocation_history[-10:]  # Last 10 events
        ]
        
        return {
            "total_events": len(self.reallocation_history),
            "total_reallocated": total_reallocated,
            "asset_frequency": asset_frequency,
            "recent_events": recent_events,
            "strategy": self.reallocation_strategy.value
        }

# Example usage
if __name__ == "__main__":
    from research_budget_allocator import ResearchAsset, ResearchStatus
    from stop_criteria_manager import StopCriteriaManager
    
    # Create test assets
    assets = {
        "asset1": ResearchAsset("asset1", "High Performer", "ml", 100000, 80000),
        "asset2": ResearchAsset("asset2", "Declining Asset", "cv", 100000, 50000),
        "asset3": ResearchAsset("asset3", "New Opportunity", "nlp", 100000, 20000),
        "asset4": ResearchAsset("asset4", "Stable Asset", "rl", 100000, 60000),
    }
    
    # Set performance data
    assets["asset1"].update_performance(0.8)
    assets["asset1"].update_performance(0.9)
    assets["asset1"].confidence_score = 0.8
    
    assets["asset2"].update_performance(0.6)
    assets["asset2"].update_performance(0.4)
    assets["asset2"].update_performance(0.3)
    assets["asset2"].confidence_score = 0.6
    
    assets["asset3"].update_performance(0.7)
    assets["asset3"].confidence_score = 0.4  # New asset, low confidence
    
    assets["asset4"].update_performance(0.6)
    assets["asset4"].update_performance(0.65)
    assets["asset4"].confidence_score = 0.7
    
    # Create reallocator
    reallocator = BudgetReallocator(strategy=ReallocationStrategy.PORTFOLIO_OPTIMIZATION)
    
    # Check if reallocation is needed
    stop_manager = StopCriteriaManager()
    stop_decisions = {}
    for asset_id, asset in assets.items():
        should_stop, decisions = stop_manager.should_stop_asset(asset)
        stop_decisions[asset_id] = decisions
    
    should_reallocate, trigger, reasoning = reallocator.should_reallocate(assets, stop_decisions)
    print(f"Should reallocate: {should_reallocate}")
    print(f"Trigger: {trigger.value}")
    print(f"Reasoning: {reasoning}")
    
    if should_reallocate:
        # Calculate reallocation
        total_available = 50000  # Example amount to reallocate
        decisions = reallocator.calculate_reallocation(assets, trigger, total_available)
        
        print(f"\nReallocation Decisions:")
        for decision in decisions:
            if abs(decision.change_amount) > 100:
                print(f"{decision.asset_id}: ${decision.change_amount:,.2f} ({decision.change_percentage:.1%}) - {decision.reasoning}")
        
        # Execute reallocation
        event = reallocator.execute_reallocation(assets, decisions)
        print(f"\nExecuted: ${event.amount_reallocated:,.2f} reallocated")
    
    # Get summary
    summary = reallocator.get_reallocation_summary()
    print(f"\nSummary:")
    print(f"Total events: {summary['total_events']}")
    print(f"Total reallocated: ${summary['total_reallocated']:,.2f}")
