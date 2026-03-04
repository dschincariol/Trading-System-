#!/usr/bin/env python3
"""
Custom Strategies Example for Research Budget Allocation System

This example demonstrates how to implement custom exploration strategies,
stop criteria, safety constraints, and reporting components.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from abc import ABC, abstractmethod

# Import base classes
from exploration_exploitation_manager import ExplorationStrategyBase
from stop_criteria_manager import StopCriterion
from safety_constraints import ConstraintValidator, SafetyConstraint
from explainable_reporting import ReportSection
from research_budget_allocator import ResearchAsset
from main_system import ResearchBudgetAllocationSystem
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================================
# CUSTOM EXPLORATION STRATEGIES
# ============================================================================

class MarketBasedExploration(ExplorationStrategyBase):
    """
    Market-based exploration strategy that treats assets like stocks in a portfolio.
    Uses concepts from market dynamics and portfolio theory.
    """
    
    def __init__(self, market_volatility: float = 0.2, risk_free_rate: float = 0.05):
        super().__init__(market_volatility=market_volatility, risk_free_rate=risk_free_rate)
        self.market_volatility = market_volatility
        self.risk_free_rate = risk_free_rate
        self.asset_prices: Dict[str, float] = {}  # Track "market prices" of assets
        self.price_history: Dict[str, List[float]] = {}
    
    def calculate_exploration_bonus(self, asset: ResearchAsset, total_assets: int, 
                                   total_trials: int) -> Tuple[float, str]:
        """Calculate exploration bonus using market-based approach"""
        
        # Initialize asset price if not exists
        if asset.id not in self.asset_prices:
            self.asset_prices[asset.id] = asset.expected_payoff
            self.price_history[asset.id] = [asset.expected_payoff]
        
        # Update price based on performance
        new_price = asset.expected_payoff
        old_price = self.asset_prices[asset.id]
        
        # Calculate "return" and volatility
        if old_price > 0:
            asset_return = (new_price - old_price) / old_price
            
            # Update price history
            self.price_history[asset.id].append(new_price)
            if len(self.price_history[asset.id]) > 20:  # Keep last 20 prices
                self.price_history[asset.id].pop(0)
            
            # Calculate asset volatility
            if len(self.price_history[asset.id]) >= 5:
                returns = [(self.price_history[asset.id][i] - self.price_history[asset.id][i-1]) / 
                          (self.price_history[asset.id][i-1] + 1e-6) 
                         for i in range(1, len(self.price_history[asset.id]))]
                asset_volatility = np.std(returns) if returns else self.market_volatility
            else:
                asset_volatility = self.market_volatility
            
            # Sharpe ratio-like exploration bonus
            excess_return = asset_return - self.risk_free_rate
            sharpe_ratio = excess_return / (asset_volatility + 1e-6)
            
            # Market-based exploration: high Sharpe ratio = good opportunity
            exploration_bonus = max(0, sharpe_ratio) * (1 - asset.confidence_score)
            
            reasoning = f"Market-based: return={asset_return:.3f}, volatility={asset_volatility:.3f}, Sharpe={sharpe_ratio:.3f}"
            
            # Update stored price
            self.asset_prices[asset.id] = new_price
            
            return exploration_bonus, reasoning
        
        return 0.0, "Insufficient price history"
    
    def update_strategy_metrics(self, asset: ResearchAsset, performance: float):
        """Update market-based metrics"""
        # Price is updated in calculate_exploration_bonus
        pass

class CollaborativeFilteringExploration(ExplorationStrategyBase):
    """
    Collaborative filtering approach that finds similar assets and uses
    their performance to inform exploration decisions.
    """
    
    def __init__(self, similarity_threshold: float = 0.7, collaboration_weight: float = 0.3):
        super().__init__(similarity_threshold=similarity_threshold, collaboration_weight=collaboration_weight)
        self.similarity_threshold = similarity_threshold
        self.collaboration_weight = collaboration_weight
        self.asset_features: Dict[str, np.ndarray] = {}
    
    def calculate_exploration_bonus(self, asset: ResearchAsset, total_assets: int, 
                                   total_trials: int) -> Tuple[float, str]:
        """Calculate exploration bonus using collaborative filtering"""
        
        # Extract features for this asset
        asset_features = self._extract_features(asset)
        self.asset_features[asset.id] = asset_features
        
        # Find similar assets
        similar_assets = self._find_similar_assets(asset.id, asset_features)
        
        if similar_assets:
            # Collaborative score based on similar assets' performance
            collaborative_score = np.mean([sim_asset['performance'] for sim_asset in similar_assets])
            
            # Weight by similarity
            weighted_score = sum(sim_asset['performance'] * sim_asset['similarity'] 
                               for sim_asset in similar_assets)
            
            # Exploration bonus for assets with high collaborative potential
            exploration_bonus = weighted_score * self.collaboration_weight * (1 - asset.confidence_score)
            
            reasoning = f"Collaborative: {len(similar_assets)} similar assets, weighted_score={weighted_score:.3f}"
            
            return exploration_bonus, reasoning
        
        # No similar assets found, use standard exploration
        exploration_bonus = asset.exploration_score * (1 - asset.confidence_score)
        reasoning = "No similar assets found, using standard exploration"
        
        return exploration_bonus, reasoning
    
    def update_strategy_metrics(self, asset: ResearchAsset, performance: float):
        """Update collaborative filtering metrics"""
        # Features are updated in calculate_exploration_bonus
        pass
    
    def _extract_features(self, asset: ResearchAsset) -> np.ndarray:
        """Extract feature vector for asset"""
        features = np.array([
            asset.expected_payoff,
            asset.confidence_score,
            len(asset.recent_performance) / 10.0,  # Normalized trial count
            asset.compute_hours_used / 1000.0,    # Normalized compute usage
            hash(asset.category) % 100 / 100.0,    # Category encoding
        ])
        return features
    
    def _find_similar_assets(self, asset_id: str, asset_features: np.ndarray) -> List[Dict]:
        """Find assets similar to the given asset"""
        similar_assets = []
        
        for other_id, other_features in self.asset_features.items():
            if other_id == asset_id:
                continue
            
            # Calculate cosine similarity
            similarity = np.dot(asset_features, other_features) / (
                np.linalg.norm(asset_features) * np.linalg.norm(other_features) + 1e-6
            )
            
            if similarity >= self.similarity_threshold:
                # Get performance data for similar asset
                # This would need access to other assets - simplified here
                similar_assets.append({
                    'asset_id': other_id,
                    'similarity': similarity,
                    'performance': 0.5  # Placeholder - would get actual performance
                })
        
        return similar_assets

# ============================================================================
# CUSTOM STOP CRITERIA
# ============================================================================

class EthicalConstraintsCriterion:
    """Custom stop criterion based on ethical considerations"""
    
    def __init__(self, ethical_threshold: float = 0.3):
        self.ethical_threshold = ethical_threshold
    
    def evaluate(self, asset: ResearchAsset, criterion: StopCriterion):
        """Evaluate ethical constraints"""
        # This would integrate with external ethical evaluation system
        # For demonstration, we'll simulate ethical scores
        
        # Simulate ethical score based on asset category and performance
        if asset.category in ['autonomous_systems', 'surveillance']:
            ethical_score = 0.2  # Lower ethical score for sensitive categories
        elif asset.category in ['healthcare', 'education']:
            ethical_score = 0.8  # Higher ethical score for beneficial categories
        else:
            ethical_score = 0.5  # Neutral score
        
        if ethical_score < self.ethical_threshold:
            from stop_criteria_manager import StopDecision, StopReason, StopSeverity
            return StopDecision(
                asset_id=asset.id,
                should_stop=True,
                reason=StopReason.EXTERNAL_STOP,  # Using external stop reason
                severity=StopSeverity.CRITICAL,
                confidence=0.9,
                details=f"Ethical score {ethical_score:.2f} below threshold {self.ethical_threshold:.2f}",
                metrics={"ethical_score": ethical_score, "threshold": self.ethical_threshold}
            )
        
        return None

class ResourceSustainabilityCriterion:
    """Custom stop criterion based on environmental sustainability"""
    
    def __init__(self, carbon_budget: float = 10000):  # kg CO2
        self.carbon_budget = carbon_budget
        self.carbon_per_compute_hour = 0.1  # kg CO2 per compute hour
    
    def evaluate(self, asset: ResearchAsset, criterion: StopCriterion):
        """Evaluate environmental sustainability"""
        carbon_used = asset.compute_hours_used * self.carbon_per_compute_hour
        
        if carbon_used > self.carbon_budget:
            from stop_criteria_manager import StopDecision, StopReason, StopSeverity
            return StopDecision(
                asset_id=asset.id,
                should_stop=True,
                reason=StopReason.RESOURCE_EXHAUSTION,
                severity=StopSeverity.WARNING,
                confidence=0.8,
                details=f"Carbon budget exceeded: {carbon_used:.1f} kg CO2 used",
                metrics={"carbon_used": carbon_used, "carbon_budget": self.carbon_budget}
            )
        
        return None

# ============================================================================
# CUSTOM SAFETY CONSTRAINTS
# ============================================================================

class EthicalConstraintValidator(ConstraintValidator):
    """Validator for ethical constraints"""
    
    def validate(self, constraint: SafetyConstraint, assets: Dict[str, ResearchAsset], 
                context: Dict[str, Any]) -> List:
        violations = []
        
        min_ethical_score = constraint.parameters.get("min_ethical_score", 0.5)
        
        for asset_id, asset in assets.items():
            # Simulate ethical evaluation
            ethical_score = self._evaluate_ethical_score(asset)
            
            if ethical_score < min_ethical_score:
                from safety_constraints import ConstraintViolation
                violations.append(ConstraintViolation(
                    constraint_name=constraint.name,
                    constraint_type=constraint.constraint_type,
                    severity=constraint.severity,
                    asset_id=asset_id,
                    value=ethical_score,
                    threshold=min_ethical_score,
                    message=f"Asset {asset.name} ethical score {ethical_score:.2f} below minimum {min_ethical_score:.2f}",
                    suggested_action=f"Review ethical implications of {asset.name}"
                ))
        
        return violations
    
    def _evaluate_ethical_score(self, asset: ResearchAsset) -> float:
        """Simulate ethical score evaluation"""
        # Simplified ethical scoring based on category
        category_scores = {
            'healthcare': 0.9,
            'education': 0.85,
            'scientific': 0.8,
            'infrastructure': 0.7,
            'ai_model': 0.6,
            'computer_vision': 0.5,
            'autonomous_systems': 0.3,
            'surveillance': 0.2
        }
        return category_scores.get(asset.category, 0.5)

class DiversityConstraintValidator(ConstraintValidator):
    """Validator for research diversity constraints"""
    
    def validate(self, constraint: SafetyConstraint, assets: Dict[str, ResearchAsset], 
                context: Dict[str, Any]) -> List:
        violations = []
        
        min_categories = constraint.parameters.get("min_categories", 3)
        max_category_dominance = constraint.parameters.get("max_category_dominance", 0.6)
        
        # Count assets by category
        category_counts = {}
        category_budgets = {}
        
        for asset in assets.values():
            category_counts[asset.category] = category_counts.get(asset.category, 0) + 1
            category_budgets[asset.category] = category_budgets.get(asset.category, 0) + asset.current_budget
        
        # Check minimum category diversity
        if len(category_counts) < min_categories:
            from safety_constraints import ConstraintViolation
            violations.append(ConstraintViolation(
                constraint_name=constraint.name,
                constraint_type=constraint.constraint_type,
                severity=constraint.severity,
                asset_id=None,
                value=len(category_counts),
                threshold=min_categories,
                message=f"Only {len(category_counts)} categories represented (minimum: {min_categories})",
                suggested_action="Add assets from underrepresented categories"
            ))
        
        # Check category dominance
        total_budget = sum(category_budgets.values())
        for category, budget in category_budgets.items():
            dominance = budget / total_budget if total_budget > 0 else 0
            if dominance > max_category_dominance:
                from safety_constraints import ConstraintViolation
                violations.append(ConstraintViolation(
                    constraint_name=constraint.name,
                    constraint_type=constraint.constraint_type,
                    severity=constraint.severity,
                    asset_id=None,
                    value=dominance,
                    threshold=max_category_dominance,
                    message=f"Category {category} dominates with {dominance:.1%} of budget",
                    suggested_action=f"Reduce allocation to {category} or add competing categories"
                ))
        
        return violations

# ============================================================================
# CUSTOM REPORTING
# ============================================================================

class CustomReportingMixin:
    """Mixin class for custom reporting functionality"""
    
    def generate_ethical_assessment_section(self, assets: Dict[str, ResearchAsset]) -> ReportSection:
        """Generate ethical assessment section"""
        
        ethical_scores = {}
        for asset_id, asset in assets.items():
            # Simulate ethical evaluation
            category_scores = {
                'healthcare': 0.9, 'education': 0.85, 'scientific': 0.8,
                'infrastructure': 0.7, 'ai_model': 0.6, 'computer_vision': 0.5,
                'autonomous_systems': 0.3, 'surveillance': 0.2
            }
            ethical_scores[asset_id] = category_scores.get(asset.category, 0.5)
        
        # Calculate overall ethical health
        avg_ethical_score = np.mean(list(ethical_scores.values()))
        high_risk_assets = [aid for aid, score in ethical_scores.items() if score < 0.4]
        
        content = f"""
        # Ethical Assessment Report
        
        ## Overall Ethical Health
        - **Average Ethical Score**: {avg_ethical_score:.2f}/1.0
        - **High-Risk Assets**: {len(high_risk_assets)}
        - **Ethically Sound Assets**: {len([s for s in ethical_scores.values() if s >= 0.7])}
        
        ## Category Breakdown
        """
        
        # Add category breakdown
        category_ethics = {}
        for asset in assets.values():
            if asset.category not in category_ethics:
                category_ethics[asset.category] = []
            category_ethics[asset.category].append(ethical_scores[asset.id])
        
        for category, scores in category_ethics.items():
            avg_score = np.mean(scores)
            content += f"- **{category}**: {avg_score:.2f} average score\n"
        
        if high_risk_assets:
            content += f"\n## High-Risk Assets\n"
            for asset_id in high_risk_assets:
                asset = assets[asset_id]
                content += f"- **{asset.name}**: Score {ethical_scores[asset_id]:.2f} - Review recommended\n"
        
        return ReportSection(
            title="Ethical Assessment",
            content=content,
            metrics={
                "avg_ethical_score": avg_ethical_score,
                "high_risk_count": len(high_risk_assets),
                "ethical_asset_count": len([s for s in ethical_scores.values() if s >= 0.7])
            },
            insights=[
                f"System ethical health is {'good' if avg_ethical_score >= 0.7 else 'needs improvement'}",
                f"{len(high_risk_assets)} assets require ethical review" if high_risk_assets else "All assets meet ethical standards"
            ]
        )
    
    def generate_sustainability_report(self, assets: Dict[str, ResearchAsset]) -> ReportSection:
        """Generate environmental sustainability report"""
        
        total_compute_hours = sum(asset.compute_hours_used for asset in assets.values())
        carbon_per_hour = 0.1  # kg CO2 per compute hour
        total_carbon = total_compute_hours * carbon_per_hour
        
        # Calculate efficiency metrics
        total_performance = sum(asset.expected_payoff for asset in assets.values())
        carbon_efficiency = total_performance / (total_carbon + 1e-6)  # Performance per kg CO2
        
        content = f"""
        # Environmental Sustainability Report
        
        ## Carbon Footprint
        - **Total Compute Hours**: {total_compute_hours:,.0f} hours
        - **Estimated CO2 Emissions**: {total_carbon:.1f} kg
        - **Carbon Efficiency**: {carbon_efficiency:.2f} performance per kg CO2
        
        ## Asset Sustainability Ranking
        """
        
        # Rank assets by carbon efficiency
        asset_efficiency = []
        for asset in assets.values():
            if asset.compute_hours_used > 0:
                efficiency = asset.expected_payoff / (asset.compute_hours_used * carbon_per_hour + 1e-6)
                asset_efficiency.append((asset.name, efficiency, asset.compute_hours_used))
        
        asset_efficiency.sort(key=lambda x: x[1], reverse=True)
        
        for i, (name, efficiency, hours) in enumerate(asset_efficiency[:5], 1):
            content += f"{i}. **{name}**: {efficiency:.2f} performance/kg CO2 ({hours:,.0f} hours)\n"
        
        return ReportSection(
            title="Environmental Sustainability",
            content=content,
            metrics={
                "total_carbon_kg": total_carbon,
                "carbon_efficiency": carbon_efficiency,
                "total_compute_hours": total_compute_hours
            },
            insights=[
                f"Total carbon footprint: {total_carbon:.1f} kg CO2",
                f"Carbon efficiency: {'high' if carbon_efficiency > 1.0 else 'moderate' if carbon_efficiency > 0.5 else 'low'}"
            ]
        )

# ============================================================================
# DEMONSTRATION
# ============================================================================

def demonstrate_custom_strategies():
    """Demonstrate all custom strategies"""
    
    print("=== Custom Strategies Demonstration ===\n")
    
    # Initialize system
    system = ResearchBudgetAllocationSystem(total_budget=1000000)
    
    # Add custom exploration strategies
    print("1. Adding custom exploration strategies...")
    
    # Market-based exploration
    market_explorer = MarketBasedExploration(market_volatility=0.15, risk_free_rate=0.03)
    system.exploration_manager.strategy = market_explorer
    
    # Add diverse assets
    system.add_research_asset("health_ai", "AI for Healthcare", "healthcare", 200000)
    system.add_research_asset("education_ai", "AI for Education", "education", 150000)
    system.add_research_asset("surveillance_system", "Smart Surveillance", "surveillance", 100000)
    system.add_research_asset("autonomous_drone", "Autonomous Drone", "autonomous_systems", 120000)
    system.add_research_asset("climate_model", "Climate Prediction", "scientific", 180000)
    
    print(f"✓ Added {len(system.assets)} assets for custom strategy testing")
    
    # Run allocation with custom exploration
    print("\n2. Running allocation with Market-Based Exploration...")
    results = system.run_allocation_cycle()
    print(f"✓ Market-based exploration completed")
    print(f"  Exploration rate: {results['exploration_metrics']['exploration_rate']:.2%}")
    
    # Switch to collaborative filtering
    print("\n3. Switching to Collaborative Filtering Exploration...")
    cf_explorer = CollaborativeFilteringExploration(similarity_threshold=0.6, collaboration_weight=0.4)
    system.exploration_manager.strategy = cf_explorer
    
    results = system.run_allocation_cycle()
    print(f"✓ Collaborative filtering exploration completed")
    print(f"  Exploration rate: {results['exploration_metrics']['exploration_rate']:.2%}")
    
    # Add custom stop criteria
    print("\n4. Adding custom stop criteria...")
    
    ethical_criterion = EthicalConstraintsCriterion(ethical_threshold=0.4)
    sustainability_criterion = ResourceSustainabilityCriterion(carbon_budget=500)
    
    # Simulate performance updates
    performance_updates = [
        ("health_ai", 0.85, 50000, 100),
        ("education_ai", 0.78, 40000, 80),
        ("surveillance_system", 0.92, 30000, 200),  # High performance but ethical concerns
        ("autonomous_drone", 0.88, 35000, 250),    # High performance but ethical concerns
        ("climate_model", 0.65, 60000, 300),
    ]
    
    for asset_id, performance, spent, compute in performance_updates:
        system.record_performance_update(asset_id, performance, spent, compute)
    
    # Test custom stop criteria
    print("\n5. Testing custom stop criteria...")
    
    for asset_id, asset in system.assets.items():
        # Test ethical criterion
        ethical_decision = ethical_criterion.evaluate(asset, None)
        if ethical_decision:
            print(f"  ✓ Ethical stop triggered for {asset.name}: {ethical_decision.details}")
        
        # Test sustainability criterion
        sustainability_decision = sustainability_criterion.evaluate(asset, None)
        if sustainability_decision:
            print(f"  ✓ Sustainability stop triggered for {asset.name}: {sustainability_decision.details}")
    
    # Add custom safety constraints
    print("\n6. Adding custom safety constraints...")
    
    from safety_constraints import SafetyConstraint, ConstraintType, ConstraintSeverity
    
    ethical_constraint = SafetyConstraint(
        name="Ethical Constraints",
        constraint_type=ConstraintType.CUSTOM,
        severity=ConstraintSeverity.ERROR,
        parameters={"min_ethical_score": 0.5}
    )
    
    diversity_constraint = SafetyConstraint(
        name="Research Diversity",
        constraint_type=ConstraintType.CUSTOM,
        severity=ConstraintSeverity.WARNING,
        parameters={"min_categories": 3, "max_category_dominance": 0.5}
    )
    
    system.safety_manager.add_constraint(ethical_constraint)
    system.safety_manager.add_constraint(diversity_constraint)
    
    # Add custom validators
    system.safety_manager.validators[ConstraintType.CUSTOM] = EthicalConstraintValidator()
    system.safety_manager.validators[ConstraintType.DIVERSITY] = DiversityConstraintValidator()
    
    # Test safety constraints
    print("\n7. Testing custom safety constraints...")
    violations = system.safety_manager.validate_constraints(system.assets, {})
    
    print(f"  Found {len(violations)} constraint violations:")
    for violation in violations:
        print(f"    - {violation.constraint_name}: {violation.message}")
    
    # Generate custom reports
    print("\n8. Generating custom reports...")
    
    # Extend reporting system with custom mixin
    class CustomReportingSystem(system.reporting_system.__class__, CustomReportingMixin):
        pass
    
    system.reporting_system.__class__ = CustomReportingSystem
    
    # Generate custom sections
    ethical_section = system.reporting_system.generate_ethical_assessment_section(system.assets)
    sustainability_section = system.reporting_system.generate_sustainability_report(system.assets)
    
    print(f"✓ Generated ethical assessment: {len(ethical_section.insights)} insights")
    print(f"✓ Generated sustainability report: {len(sustainability_section.insights)} insights")
    
    # Show custom strategy insights
    print("\n9. Custom Strategy Insights:")
    print(f"  Market-based prices tracked: {len(market_explorer.asset_prices)} assets")
    print(f"  Collaborative similarities found: {len(cf_explorer.asset_features)} assets")
    print(f"  Ethical concerns identified: {len([v for v in violations if 'ethical' in v.constraint_name.lower()])}")
    print(f"  Diversity concerns identified: {len([v for v in violations if 'diversity' in v.constraint_name.lower()])}")
    
    print("\n=== Custom Strategies Demonstration Completed! ===")
    print("\nCustom components demonstrated:")
    print("• Market-Based Exploration Strategy")
    print("• Collaborative Filtering Exploration Strategy")
    print("• Ethical Constraints Stop Criterion")
    print("• Resource Sustainability Stop Criterion")
    print("• Ethical Constraint Validator")
    print("• Diversity Constraint Validator")
    print("• Custom Ethical Assessment Reporting")
    print("• Custom Sustainability Reporting")

if __name__ == "__main__":
    demonstrate_custom_strategies()
