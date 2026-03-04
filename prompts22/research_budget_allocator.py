"""
Research Budget Allocation System

A deterministic and explainable system for allocating research resources
based on expected payoff, balancing exploration vs exploitation, and
automatically stopping unproductive research paths.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from enum import Enum
import logging
from datetime import datetime, timedelta
import json

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ResearchStatus(Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"

class AllocationStrategy(Enum):
    EXPECTED_PAYOFF = "expected_payoff"
    EXPLORATION_BONUS = "exploration_bonus"
    BALANCED = "balanced"

@dataclass
class ResearchAsset:
    """Represents a research asset/model/horizon being explored"""
    id: str
    name: str
    category: str
    initial_budget: float
    current_budget: float
    total_spent: float = 0.0
    status: ResearchStatus = ResearchStatus.ACTIVE
    start_time: datetime = field(default_factory=datetime.now)
    last_update: datetime = field(default_factory=datetime.now)
    
    # Performance metrics
    expected_payoff: float = 0.0
    confidence_score: float = 0.5  # 0-1, increases with more data
    recent_performance: List[float] = field(default_factory=list)
    exploration_score: float = 1.0  # Higher for newer/less explored assets
    
    # Resource utilization
    compute_hours_used: float = 0.0
    data_processed: float = 0.0
    
    def update_performance(self, performance_metric: float):
        """Update performance metrics with new data point"""
        self.recent_performance.append(performance_metric)
        if len(self.recent_performance) > 10:  # Keep only recent 10 metrics
            self.recent_performance.pop(0)
        
        # Update confidence based on data points
        self.confidence_score = min(1.0, len(self.recent_performance) / 10.0)
        
        # Update expected payoff (exponential moving average)
        if self.expected_payoff == 0.0:
            self.expected_payoff = performance_metric
        else:
            alpha = 0.3  # Smoothing factor
            self.expected_payoff = alpha * performance_metric + (1 - alpha) * self.expected_payoff
    
    def spend_budget(self, amount: float, compute_hours: float = 0.0, data_amount: float = 0.0):
        """Record budget spending and resource usage"""
        spent = min(amount, self.current_budget)
        self.current_budget -= spent
        self.total_spent += spent
        self.compute_hours_used += compute_hours
        self.data_processed += data_amount
        self.last_update = datetime.now()
        return spent

@dataclass
class AllocationDecision:
    """Represents an allocation decision with explainable reasoning"""
    asset_id: str
    allocated_amount: float
    reasoning: str
    expected_payoff: float
    exploration_bonus: float
    risk_score: float
    timestamp: datetime = field(default_factory=datetime.now)

class ResearchBudgetAllocator:
    """Main budget allocation system"""
    
    def __init__(self, total_budget: float, allocation_strategy: AllocationStrategy = AllocationStrategy.BALANCED):
        self.total_budget = total_budget
        self.available_budget = total_budget
        self.allocation_strategy = allocation_strategy
        self.assets: Dict[str, ResearchAsset] = {}
        self.allocation_history: List[AllocationDecision] = []
        
        # Safety constraints
        self.min_allocation_per_asset = total_budget * 0.01  # 1% minimum
        self.max_allocation_per_asset = total_budget * 0.4   # 40% maximum
        self.max_total_spent_ratio = 0.9  # Can't spend more than 90% of total budget
        
        # Exploration parameters
        self.exploration_decay_rate = 0.95  # Exploration score decays over time
        self.exploration_boost_new_assets = 2.0  # New assets get exploration boost
        
        # Stop criteria parameters
        self.min_performance_threshold = 0.1
        self.max_consecutive_failures = 5
        self.min_budget_threshold = total_budget * 0.005  # 0.5% minimum remaining
        
        logger.info(f"Initialized ResearchBudgetAllocator with ${total_budget:,.2f} total budget")
    
    def add_asset(self, asset: ResearchAsset):
        """Add a new research asset to the system"""
        if asset.id in self.assets:
            logger.warning(f"Asset {asset.id} already exists, updating...")
            self.assets[asset.id] = asset
        else:
            # Give new assets exploration bonus
            asset.exploration_score = self.exploration_boost_new_assets
            self.assets[asset.id] = asset
            logger.info(f"Added new asset: {asset.name} (${asset.initial_budget:,.2f})")
    
    def calculate_allocation_score(self, asset: ResearchAsset) -> Tuple[float, str]:
        """
        Calculate allocation score for an asset based on strategy
        Returns: (score, reasoning)
        """
        if asset.status != ResearchStatus.ACTIVE:
            return 0.0, "Asset is not active"
        
        base_score = asset.expected_payoff * asset.confidence_score
        exploration_bonus = asset.exploration_score * (1 - asset.confidence_score)
        
        reasoning_parts = []
        
        if self.allocation_strategy == AllocationStrategy.EXPECTED_PAYOFF:
            score = base_score
            reasoning_parts.append(f"Expected payoff: {asset.expected_payoff:.3f}")
            reasoning_parts.append(f"Confidence: {asset.confidence_score:.3f}")
        
        elif self.allocation_strategy == AllocationStrategy.EXPLORATION_BONUS:
            score = exploration_bonus
            reasoning_parts.append(f"Exploration score: {asset.exploration_score:.3f}")
            reasoning_parts.append(f"Uncertainty bonus: {1 - asset.confidence_score:.3f}")
        
        else:  # BALANCED
            # Weighted combination of exploitation and exploration
            exploitation_weight = 0.7
            exploration_weight = 0.3
            score = (exploitation_weight * base_score + exploration_weight * exploration_bonus)
            reasoning_parts.append(f"Exploitation (70%): {base_score:.3f}")
            reasoning_parts.append(f"Exploration (30%): {exploration_bonus:.3f}")
        
        # Apply risk adjustment (reduce score for high-risk assets)
        risk_score = self.calculate_risk_score(asset)
        adjusted_score = score * (1 - risk_score)
        
        if risk_score > 0.1:
            reasoning_parts.append(f"Risk adjustment: -{risk_score:.2%}")
        
        reasoning = "; ".join(reasoning_parts)
        return adjusted_score, reasoning
    
    def calculate_risk_score(self, asset: ResearchAsset) -> float:
        """Calculate risk score for an asset (0 = no risk, 1 = high risk)"""
        risk_factors = []
        
        # Budget utilization risk
        if asset.current_budget < self.min_budget_threshold:
            risk_factors.append(0.3)  # Low budget is risky
        
        # Performance volatility risk
        if len(asset.recent_performance) >= 3:
            recent_perf = asset.recent_performance[-3:]
            volatility = np.std(recent_perf) / (np.mean(recent_perf) + 1e-6)
            risk_factors.append(min(volatility, 0.5))
        
        # Stagnation risk
        if asset.last_update < datetime.now() - timedelta(days=7):
            risk_factors.append(0.2)  # Stagnant assets are riskier
        
        return min(sum(risk_factors), 0.8)  # Cap at 80% risk
    
    def should_stop_asset(self, asset: ResearchAsset) -> Tuple[bool, str]:
        """
        Determine if an asset should be stopped based on stop criteria
        Returns: (should_stop, reason)
        """
        stop_reasons = []
        
        # Performance-based stopping
        if len(asset.recent_performance) >= self.max_consecutive_failures:
            recent_failures = sum(1 for p in asset.recent_performance[-self.max_consecutive_failures:] 
                                if p < self.min_performance_threshold)
            if recent_failures >= self.max_consecutive_failures:
                stop_reasons.append(f"{self.max_consecutive_failures} consecutive failures")
        
        # Budget-based stopping
        if asset.current_budget < self.min_budget_threshold:
            stop_reasons.append("Insufficient remaining budget")
        
        # ROI-based stopping
        if asset.total_spent > 0 and asset.expected_payoff > 0:
            roi = asset.expected_payoff / (asset.total_spent + 1e-6)
            if roi < 0.1:  # ROI less than 10%
                stop_reasons.append("Low return on investment")
        
        # Confidence-based stopping
        if asset.confidence_score > 0.8 and asset.expected_payoff < self.min_performance_threshold:
            stop_reasons.append("High confidence in poor performance")
        
        should_stop = len(stop_reasons) > 0
        reason = "; ".join(stop_reasons) if stop_reasons else ""
        
        return should_stop, reason
    
    def allocate_budget(self) -> List[AllocationDecision]:
        """
        Main allocation logic - distribute available budget among active assets
        """
        if self.available_budget <= 0:
            logger.warning("No available budget for allocation")
            return []
        
        # Check for assets that should be stopped
        assets_to_stop = []
        for asset_id, asset in self.assets.items():
            should_stop, reason = self.should_stop_asset(asset)
            if should_stop:
                assets_to_stop.append((asset_id, reason))
                asset.status = ResearchStatus.STOPPED
                logger.info(f"Stopping asset {asset.name}: {reason}")
        
        # Reallocate budget from stopped assets
        for asset_id, reason in assets_to_stop:
            asset = self.assets[asset_id]
            self.available_budget += asset.current_budget
            asset.current_budget = 0
        
        # Calculate allocation scores for all active assets
        active_assets = [asset for asset in self.assets.values() if asset.status == ResearchStatus.ACTIVE]
        
        if not active_assets:
            logger.info("No active assets for allocation")
            return []
        
        # Calculate scores and reasoning for each asset
        asset_scores = []
        for asset in active_assets:
            score, reasoning = self.calculate_allocation_score(asset)
            asset_scores.append((asset, score, reasoning))
        
        # Normalize scores to get allocation percentages
        total_score = sum(score for _, score, _ in asset_scores)
        
        if total_score <= 0:
            logger.warning("All assets have zero scores, using equal allocation")
            allocation_per_asset = self.available_budget / len(active_assets)
            decisions = []
            for asset, _, _ in asset_scores:
                allocated = min(allocation_per_asset, self.max_allocation_per_asset)
                decision = AllocationDecision(
                    asset_id=asset.id,
                    allocated_amount=allocated,
                    reasoning="Equal allocation (all scores zero)",
                    expected_payoff=asset.expected_payoff,
                    exploration_bonus=asset.exploration_score,
                    risk_score=self.calculate_risk_score(asset)
                )
                decisions.append(decision)
                asset.current_budget += allocated
                self.available_budget -= allocated
        else:
            decisions = []
            for asset, score, reasoning in asset_scores:
                # Calculate allocation percentage
                allocation_percentage = score / total_score
                allocated_amount = self.available_budget * allocation_percentage
                
                # Apply safety constraints
                allocated_amount = max(self.min_allocation_per_asset, allocated_amount)
                allocated_amount = min(self.max_allocation_per_asset, allocated_amount)
                allocated_amount = min(allocated_amount, self.available_budget)
                
                if allocated_amount > 0:
                    decision = AllocationDecision(
                        asset_id=asset.id,
                        allocated_amount=allocated_amount,
                        reasoning=reasoning,
                        expected_payoff=asset.expected_payoff,
                        exploration_bonus=asset.exploration_score,
                        risk_score=self.calculate_risk_score(asset)
                    )
                    decisions.append(decision)
                    asset.current_budget += allocated_amount
                    self.available_budget -= allocated_amount
        
        # Update exploration scores (decay over time)
        for asset in active_assets:
            asset.exploration_score *= self.exploration_decay_rate
        
        # Record allocation decisions
        self.allocation_history.extend(decisions)
        
        logger.info(f"Allocated ${sum(d.allocated_amount for d in decisions):,.2f} across {len(decisions)} assets")
        return decisions
    
    def record_expenditure(self, asset_id: str, amount: float, performance_metric: float, 
                          compute_hours: float = 0.0, data_amount: float = 0.0):
        """Record expenditure and update asset performance"""
        if asset_id not in self.assets:
            logger.error(f"Asset {asset_id} not found")
            return
        
        asset = self.assets[asset_id]
        spent = asset.spend_budget(amount, compute_hours, data_amount)
        asset.update_performance(performance_metric)
        
        logger.info(f"Asset {asset.name}: spent ${spent:,.2f}, performance: {performance_metric:.3f}")
    
    def get_allocation_summary(self) -> Dict:
        """Get comprehensive allocation summary"""
        summary = {
            "total_budget": self.total_budget,
            "available_budget": self.available_budget,
            "allocated_budget": self.total_budget - self.available_budget,
            "total_assets": len(self.assets),
            "active_assets": len([a for a in self.assets.values() if a.status == ResearchStatus.ACTIVE]),
            "stopped_assets": len([a for a in self.assets.values() if a.status == ResearchStatus.STOPPED]),
            "allocation_strategy": self.allocation_strategy.value,
            "assets": []
        }
        
        for asset in self.assets.values():
            asset_summary = {
                "id": asset.id,
                "name": asset.name,
                "category": asset.category,
                "status": asset.status.value,
                "current_budget": asset.current_budget,
                "total_spent": asset.total_spent,
                "expected_payoff": asset.expected_payoff,
                "confidence_score": asset.confidence_score,
                "exploration_score": asset.exploration_score,
                "risk_score": self.calculate_risk_score(asset)
            }
            summary["assets"].append(asset_summary)
        
        return summary
    
    def export_allocation_report(self, filename: str = None) -> str:
        """Export detailed allocation report to JSON"""
        if filename is None:
            filename = f"allocation_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        report = {
            "timestamp": datetime.now().isoformat(),
            "summary": self.get_allocation_summary(),
            "recent_decisions": [
                {
                    "timestamp": d.timestamp.isoformat(),
                    "asset_id": d.asset_id,
                    "allocated_amount": d.allocated_amount,
                    "reasoning": d.reasoning,
                    "expected_payoff": d.expected_payoff,
                    "exploration_bonus": d.exploration_bonus,
                    "risk_score": d.risk_score
                }
                for d in self.allocation_history[-20:]  # Last 20 decisions
            ]
        }
        
        with open(filename, 'w') as f:
            json.dump(report, f, indent=2)
        
        logger.info(f"Allocation report exported to {filename}")
        return filename

# Example usage and testing
if __name__ == "__main__":
    # Initialize the allocator
    allocator = ResearchBudgetAllocator(total_budget=1000000, allocation_strategy=AllocationStrategy.BALANCED)
    
    # Add some research assets
    assets = [
        ResearchAsset("model_1", "GPT-4 Fine-tuning", "language_model", 200000, 0),
        ResearchAsset("model_2", "Vision Transformer", "computer_vision", 150000, 0),
        ResearchAsset("model_3", "Reinforcement Learning", "rl_agent", 100000, 0),
        ResearchAsset("horizon_1", "Long-term Planning", "planning", 250000, 0),
        ResearchAsset("asset_1", "Data Pipeline Optimization", "infrastructure", 180000, 0),
    ]
    
    for asset in assets:
        allocator.add_asset(asset)
    
    # Simulate initial allocation
    print("=== Initial Allocation ===")
    decisions = allocator.allocate_budget()
    for decision in decisions:
        asset = allocator.assets[decision.asset_id]
        print(f"{asset.name}: ${decision.allocated_amount:,.2f} - {decision.reasoning}")
    
    # Simulate some performance updates
    print("\n=== Performance Updates ===")
    performance_data = [
        ("model_1", 50000, 0.8, 100, 1000),
        ("model_2", 30000, 0.6, 80, 800),
        ("model_3", 20000, 0.3, 120, 1200),
        ("horizon_1", 40000, 0.7, 90, 900),
        ("asset_1", 35000, 0.9, 60, 600),
    ]
    
    for asset_id, amount, performance, compute_hours, data_amount in performance_data:
        allocator.record_expenditure(asset_id, amount, performance, compute_hours, data_amount)
    
    # Reallocate based on performance
    print("\n=== Re-allocation ===")
    decisions = allocator.allocate_budget()
    for decision in decisions:
        asset = allocator.assets[decision.asset_id]
        print(f"{asset.name}: ${decision.allocated_amount:,.2f} - {decision.reasoning}")
    
    # Generate summary
    print("\n=== Allocation Summary ===")
    summary = allocator.get_allocation_summary()
    print(f"Total Budget: ${summary['total_budget']:,.2f}")
    print(f"Available: ${summary['available_budget']:,.2f}")
    print(f"Active Assets: {summary['active_assets']}")
    print(f"Stopped Assets: {summary['stopped_assets']}")
    
    # Export report
    report_file = allocator.export_allocation_report()
    print(f"\nReport exported to: {report_file}")
