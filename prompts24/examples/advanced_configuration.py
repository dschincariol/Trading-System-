#!/usr/bin/env python3
"""
Advanced Configuration Example for Research Budget Allocation System

This example demonstrates advanced configuration options, custom strategies,
and sophisticated usage patterns of the research budget allocation system.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main_system import ResearchBudgetAllocationSystem
from research_budget_allocator import AllocationStrategy
from exploration_exploitation_manager import ExplorationStrategy
from budget_reallocator import ReallocationStrategy
from safety_constraints import DeterministicLevel
from stop_criteria_manager import StopCriterion, StopReason, StopSeverity
from feedback_monitor import SignalType, AlertLevel
import logging
import numpy as np

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def create_advanced_config():
    """Create advanced configuration for the system"""
    config = {
        # Core allocation settings
        'allocation_strategy': 'BALANCED',  # or EXPECTED_PAYOFF, EXPLORATION_BONUS
        
        # Advanced exploration settings
        'exploration_strategy': 'BAYESIAN_OPTIMIZATION',
        'exploration_params': {
            'acquisition_function': 'ei',  # Expected Improvement
            'length_scale': 1.0
        },
        
        # Sophisticated reallocation
        'reallocation_strategy': 'PORTFOLIO_OPTIMIZATION',
        'reallocation_params': {
            'min_reallocation_amount': 5000,  # Higher minimum for larger budgets
            'max_reallocation_frequency': 5,   # More frequent reallocation
            'max_budget_change_percentage': 0.2  # Conservative changes
        },
        
        # Enhanced monitoring
        'monitoring_window_size': 200,
        'alert_threshold': 1.5,  # More sensitive alerts
        
        # Full determinism for reproducibility
        'deterministic_level': 'FULL',
        
        # Automated features
        'auto_reallocate': True,
        'auto_stop': True,
        'reporting_frequency': 'weekly',
        
        # Output settings
        'reports_output_dir': 'advanced_reports'
    }
    
    return config

def setup_custom_stop_criteria(stop_manager):
    """Setup custom stop criteria for advanced scenarios"""
    
    # Custom performance decline criterion
    stop_manager.update_criterion_threshold("performance_threshold", 0.15)  # Higher threshold
    stop_manager.update_criterion_threshold("consecutive_failures", 3)     # Fewer failures allowed
    
    # Custom ROI criterion
    stop_manager.update_criterion_threshold("roi_threshold", 0.2)  # Higher ROI requirement
    
    # Add custom volatility-based stopping
    def volatility_stop_criterion(asset, criterion):
        """Custom criterion: stop if performance volatility is too high"""
        if len(asset.recent_performance) < 8:
            return None
        
        recent_perf = asset.recent_performance[-8:]
        volatility = np.std(recent_perf) / (np.mean(recent_perf) + 1e-6)
        
        if volatility > criterion.threshold:
            from stop_criteria_manager import StopDecision
            return StopDecision(
                asset_id=asset.id,
                should_stop=True,
                reason=StopReason.POOR_PERFORMANCE,
                severity=criterion.severity,
                confidence=min(1.0, volatility / criterion.threshold),
                details=f"Performance volatility {volatility:.3f} exceeds threshold {criterion.threshold:.3f}",
                metrics={"volatility": volatility, "threshold": criterion.threshold}
            )
        return None
    
    stop_manager.add_custom_criterion(
        "high_volatility",
        volatility_stop_criterion,
        threshold=0.8,  # 80% volatility threshold
        severity=StopSeverity.WARNING
    )

def setup_custom_safety_constraints(safety_manager):
    """Setup custom safety constraints"""
    
    # Update budget limits for larger scale
    safety_manager.update_constraint_parameters("total_budget_limit", {"max_total_budget": 10000000})
    safety_manager.update_constraint_parameters("individual_asset_cap", {"max_percentage": 0.3})  # 30% max per asset
    
    # Add concentration risk constraint
    safety_manager.update_constraint_parameters("concentration_risk", {
        "max_top_assets": 2,      # Top 2 assets only
        "max_top_percentage": 0.6  # 60% max concentration
    })
    
    # Add velocity limit for rapid changes
    safety_manager.update_constraint_parameters("velocity_limit", {
        "max_change_percentage": 0.15,  # 15% max change per cycle
        "time_window_hours": 12
    })

def simulate_complex_research_scenario():
    """Simulate a complex research scenario with multiple asset types"""
    
    print("=== Advanced Configuration Example ===\n")
    
    # Create advanced configuration
    config = create_advanced_config()
    print("1. Created advanced configuration with:")
    print(f"   • Allocation strategy: {config['allocation_strategy']}")
    print(f"   • Exploration strategy: {config['exploration_strategy']}")
    print(f"   • Reallocation strategy: {config['reallocation_strategy']}")
    print(f"   • Deterministic level: {config['deterministic_level']}")
    
    # Initialize system with advanced config
    print("\n2. Initializing system with $5M budget and advanced config...")
    system = ResearchBudgetAllocationSystem(total_budget=5000000, config=config)
    
    # Setup custom components
    print("3. Setting up custom stop criteria and safety constraints...")
    setup_custom_stop_criteria(system.stop_manager)
    setup_custom_safety_constraints(system.safety_manager)
    
    # Add diverse research assets
    print("\n4. Adding diverse research assets...")
    
    # Large-scale AI models
    system.add_research_asset("llm_foundation", "Large Language Model Foundation", "ai_model", 1500000)
    system.add_research_asset("multimodal_gpt", "Multimodal GPT Variant", "ai_model", 1200000)
    system.add_research_asset("code_generation", "Code Generation Model", "ai_model", 1000000)
    
    # Computer Vision assets
    system.add_research_asset("3d_vision", "3D Computer Vision", "computer_vision", 800000)
    system.add_research_asset("video_understanding", "Video Understanding", "computer_vision", 600000)
    
    # Scientific Research assets
    system.add_research_asset("drug_discovery", "AI for Drug Discovery", "scientific", 700000)
    system.add_research_asset("climate_modeling", "Climate Prediction Model", "scientific", 500000)
    
    # Infrastructure assets
    system.add_research_asset("distributed_training", "Distributed Training Platform", "infrastructure", 400000)
    system.add_research_asset("data_pipeline_v2", "Advanced Data Pipeline", "infrastructure", 300000)
    
    print(f"✓ Added {len(system.assets)} research assets across 4 categories")
    
    # Run initial allocation with advanced features
    print("\n5. Running initial allocation with advanced features...")
    initial_results = system.run_allocation_cycle()
    
    print(f"✓ Allocated ${initial_results['total_allocated']:,.2f}")
    print(f"✓ Exploration rate: {initial_results['exploration_metrics']['exploration_rate']:.2%}")
    print(f"✓ Exploitation rate: {initial_results['exploration_metrics']['exploitation_rate']:.2%}")
    
    # Simulate complex performance patterns
    print("\n6. Simulating complex performance patterns...")
    
    # Different performance trajectories for different asset types
    performance_scenarios = [
        # High-performing AI models with good confidence
        ("llm_foundation", [0.7, 0.75, 0.82, 0.85, 0.88], 450000, 800),
        ("multimodal_gpt", [0.65, 0.72, 0.78, 0.83, 0.86], 380000, 750),
        ("code_generation", [0.8, 0.83, 0.85, 0.87, 0.89], 320000, 600),
        
        # Moderate computer vision with volatility
        ("3d_vision", [0.6, 0.55, 0.68, 0.62, 0.71], 200000, 400),
        ("video_understanding", [0.5, 0.58, 0.52, 0.65, 0.61], 150000, 350),
        
        # Scientific research with slow but steady progress
        ("drug_discovery", [0.4, 0.45, 0.48, 0.52, 0.55], 180000, 900),
        ("climate_modeling", [0.35, 0.42, 0.46, 0.49, 0.53], 120000, 1200),
        
        # Infrastructure with high reliability
        ("distributed_training", [0.9, 0.92, 0.91, 0.93, 0.94], 80000, 200),
        ("data_pipeline_v2", [0.85, 0.87, 0.89, 0.88, 0.90], 60000, 150),
    ]
    
    for asset_id, performance_trajectory, total_spent, compute_hours in performance_scenarios:
        asset = system.assets[asset_id]
        
        # Simulate gradual performance updates
        for i, performance in enumerate(performance_trajectory):
            spent_per_update = total_spent // len(performance_trajectory)
            compute_per_update = compute_hours // len(performance_trajectory)
            
            system.record_performance_update(
                asset_id, performance, spent_per_update, compute_per_update, 
                spent_per_update * 10  # Data amount proportional to spend
            )
        
        print(f"  ✓ {asset.name}: final performance {performance_trajectory[-1]:.2f}")
    
    # Run multiple allocation cycles to see adaptation
    print("\n7. Running multiple allocation cycles to observe adaptation...")
    
    for cycle in range(3):
        print(f"\n   Cycle {cycle + 1}:")
        results = system.run_allocation_cycle()
        
        print(f"     Allocated: ${results['total_allocated']:,.2f}")
        print(f"     Stop decisions: {results['stop_decisions']}")
        print(f"     Reallocation events: {results['reallocation_events']}")
        print(f"     Safety violations: {results['safety_violations']}")
        
        # Show top performers
        top_assets = sorted(system.assets.values(), 
                          key=lambda a: a.expected_payoff, reverse=True)[:3]
        print(f"     Top performers: {[a.name for a in top_assets]}")
    
    # Demonstrate advanced monitoring
    print("\n8. Advanced monitoring and alerting...")
    
    # Get recent signals by type
    performance_signals = system.monitor.get_recent_signals(signal_type=SignalType.PERFORMANCE, limit=5)
    budget_signals = system.monitor.get_recent_signals(signal_type=SignalType.BUDGET_UTILIZATION, limit=5)
    
    print(f"  Recent performance signals: {len(performance_signals)}")
    for signal in performance_signals:
        print(f"    {signal.asset_id}: {signal.message}")
    
    print(f"  Recent budget signals: {len(budget_signals)}")
    for signal in budget_signals:
        print(f"    {signal.asset_id}: {signal.message}")
    
    # Show active alerts
    active_alerts = system.monitor.get_active_alerts()
    print(f"  Active alerts: {len(active_alerts)}")
    for alert in active_alerts[:3]:  # Show top 3
        print(f"    {alert.level.value}: {alert.title}")
    
    # Demonstrate strategy switching
    print("\n9. Demonstrating strategy switching...")
    
    # Switch to more aggressive exploration
    print("   Switching to aggressive exploration strategy...")
    system.exploration_manager.switch_strategy(ExplorationStrategy.EPSILON_GREEDY, 
                                             initial_epsilon=0.4, decay_rate=0.98)
    
    # Run a cycle with new strategy
    results = system.run_allocation_cycle()
    print(f"   New exploration rate: {results['exploration_metrics']['exploration_rate']:.2%}")
    
    # Switch to conservative reallocation
    print("   Switching to conservative reallocation strategy...")
    system.reallocator.reallocation_strategy = ReallocationStrategy.CONSERVATIVE_REBALANCE
    
    results = system.run_allocation_cycle()
    print(f"   Conservative reallocation completed")
    
    # Generate comprehensive reports
    print("\n10. Generating comprehensive reports...")
    
    try:
        # Technical report for system analysis
        tech_report = system.generate_system_report(report_type="technical")
        print(f"✓ Technical report: {tech_report}")
        
        # Compliance report for audit
        compliance_report = system.generate_system_report(report_type="compliance")
        print(f"✓ Compliance report: {compliance_report}")
        
        # Detailed report for full analysis
        detailed_report = system.generate_system_report(report_type="detailed")
        print(f"✓ Detailed report: {detailed_report}")
        
    except Exception as e:
        print(f"⚠ Report generation failed: {e}")
    
    # Show final system state
    print("\n11. Final system state analysis...")
    
    status = system.get_system_status()
    print(f"  Total budget utilized: ${(status['total_budget'] - status['available_budget']):,.2f}")
    print(f"  Active assets: {status['active_assets']}/{status['total_assets']}")
    print(f"  System efficiency: {status['allocation_summary']['allocated_budget']/status['total_budget']:.1%}")
    
    # Asset performance ranking
    print("\n12. Final asset performance ranking:")
    ranked_assets = sorted(system.assets.values(), 
                          key=lambda a: a.expected_payoff * a.confidence_score, reverse=True)
    
    print(f"{'Rank':<4} {'Asset':<25} {'Performance':<12} {'Confidence':<11} {'Budget':<12} {'ROI':<8}")
    print("-" * 80)
    
    for i, asset in enumerate(ranked_assets, 1):
        roi = (asset.expected_payoff / (asset.total_spent / 1000)) if asset.total_spent > 0 else 0
        print(f"{i:<4} {asset.name:<25} {asset.expected_payoff:<11.3f} {asset.confidence_score:<10.2f} "
              f"${asset.current_budget:<11,.0f} {roi:<7.2f}")
    
    # Export complete system state
    print("\n13. Exporting complete system state...")
    try:
        state_path = system.export_system_state("advanced_system_state.json")
        print(f"✓ System state exported: {state_path}")
    except Exception as e:
        print(f"⚠ State export failed: {e}")
    
    print("\n=== Advanced Configuration Example Completed! ===")
    print("\nKey advanced features demonstrated:")
    print("• Custom configuration with multiple strategies")
    print("• Custom stop criteria and safety constraints")
    print("• Complex performance trajectories")
    print("• Multi-cycle adaptation and learning")
    print("• Advanced monitoring and alerting")
    print("• Dynamic strategy switching")
    print("• Comprehensive reporting and analysis")
    print("• Full system state export/import")

if __name__ == "__main__":
    simulate_complex_research_scenario()
