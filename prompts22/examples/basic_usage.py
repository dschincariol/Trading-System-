#!/usr/bin/env python3
"""
Basic Usage Example for Research Budget Allocation System

This example demonstrates the core functionality of the research budget allocation system
with a simple scenario involving multiple research assets.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main_system import ResearchBudgetAllocationSystem
import logging

# Configure logging for this example
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def main():
    """Main example function"""
    print("=== Research Budget Allocation System - Basic Usage Example ===\n")
    
    # Initialize the system with $2M total budget
    print("1. Initializing system with $2,000,000 budget...")
    system = ResearchBudgetAllocationSystem(total_budget=2000000)
    
    # Add various research assets
    print("\n2. Adding research assets...")
    
    # Machine Learning assets
    system.add_research_asset(
        asset_id="gpt4_finetune",
        name="GPT-4 Fine-tuning",
        category="language_model",
        initial_budget=500000
    )
    
    system.add_research_asset(
        asset_id="vision_transformer",
        name="Vision Transformer v2",
        category="computer_vision", 
        initial_budget=400000
    )
    
    system.add_research_asset(
        asset_id="multimodal_fusion",
        name="Multimodal Fusion Network",
        category="multimodal",
        initial_budget=350000
    )
    
    # Reinforcement Learning assets
    system.add_research_asset(
        asset_id="rl_robotics",
        name="RL for Robotics Control",
        category="reinforcement_learning",
        initial_budget=300000
    )
    
    system.add_research_asset(
        asset_id="game_ai",
        name="Game Playing AI Agent",
        category="reinforcement_learning",
        initial_budget=200000
    )
    
    # Infrastructure and Data assets
    system.add_research_asset(
        asset_id="data_pipeline",
        name="Data Pipeline Optimization",
        category="infrastructure",
        initial_budget=150000
    )
    
    system.add_research_asset(
        asset_id="compute_optimization",
        name="Compute Resource Optimization",
        category="infrastructure",
        initial_budget=100000
    )
    
    print(f"Added {len(system.assets)} research assets")
    
    # Run initial allocation cycle
    print("\n3. Running initial allocation cycle...")
    initial_results = system.run_allocation_cycle()
    
    print(f"✓ Allocated ${initial_results['total_allocated']:,.2f} across {initial_results['allocation_decisions']} assets")
    print(f"✓ Active assets: {initial_results['active_assets']}")
    print(f"✓ Cycle duration: {initial_results['duration_seconds']:.2f} seconds")
    
    # Show initial allocation details
    print("\n4. Initial allocation details:")
    for asset_id, asset in system.assets.items():
        if asset.current_budget > 0:
            print(f"  {asset.name}: ${asset.current_budget:,.2f} ({asset.current_budget/asset.initial_budget:.1%} of initial)")
    
    # Simulate performance updates over time
    print("\n5. Simulating performance updates...")
    
    performance_updates = [
        # (asset_id, performance, amount_spent, compute_hours, data_amount)
        ("gpt4_finetune", 0.85, 120000, 200, 2000),
        ("vision_transformer", 0.72, 80000, 150, 1500),
        ("multimodal_fusion", 0.68, 70000, 180, 1800),
        ("rl_robotics", 0.45, 60000, 300, 500),
        ("game_ai", 0.78, 40000, 120, 800),
        ("data_pipeline", 0.92, 30000, 50, 3000),
        ("compute_optimization", 0.88, 20000, 30, 200),
    ]
    
    for asset_id, performance, spent, compute, data in performance_updates:
        success = system.record_performance_update(asset_id, performance, spent, compute, data)
        if success:
            asset = system.assets[asset_id]
            print(f"  ✓ {asset.name}: performance {performance:.2f}, spent ${spent:,.2f}")
        else:
            print(f"  ✗ Failed to update {asset_id}")
    
    # Run second allocation cycle with performance data
    print("\n6. Running second allocation cycle with performance data...")
    second_results = system.run_allocation_cycle()
    
    print(f"✓ Allocated ${second_results['total_allocated']:,.2f} across {second_results['allocation_decisions']} assets")
    print(f"✓ Stop decisions: {second_results['stop_decisions']}")
    print(f"✓ Reallocation events: {second_results['reallocation_events']}")
    
    # Show updated allocation details
    print("\n7. Updated allocation details:")
    for asset_id, asset in system.assets.items():
        if asset.current_budget > 0:
            print(f"  {asset.name}: ${asset.current_budget:,.2f} (performance: {asset.expected_payoff:.2f}, confidence: {asset.confidence_score:.2f})")
    
    # Get system status
    print("\n8. System status:")
    status = system.get_system_status()
    print(f"  Total budget: ${status['total_budget']:,.2f}")
    print(f"  Available budget: ${status['available_budget']:,.2f}")
    print(f"  Active assets: {status['active_assets']}")
    print(f"  Allocation strategy: {status['allocation_strategy']}")
    print(f"  Exploration strategy: {status['exploration_strategy']}")
    print(f"  Active alerts: {status['active_alerts_count']}")
    print(f"  Critical alerts: {status['critical_alerts']}")
    
    # Generate reports
    print("\n9. Generating reports...")
    
    try:
        # Generate summary report
        summary_report = system.generate_system_report()
        print(f"✓ Summary report: {summary_report}")
        
        # Generate detailed report
        detailed_report = system.generate_system_report(report_type="detailed")
        print(f"✓ Detailed report: {detailed_report}")
        
        # Generate executive report
        executive_report = system.generate_system_report(report_type="executive")
        print(f"✓ Executive report: {executive_report}")
        
    except Exception as e:
        print(f"⚠ Report generation failed: {e}")
    
    # Export system state
    print("\n10. Exporting system state...")
    try:
        state_path = system.export_system_state()
        print(f"✓ System state exported: {state_path}")
    except Exception as e:
        print(f"⚠ State export failed: {e}")
    
    # Show exploration metrics
    print("\n11. Exploration vs Exploitation metrics:")
    exploration_metrics = system.exploration_manager.get_metrics()
    print(f"  Exploration rate: {exploration_metrics.exploration_rate:.2%}")
    print(f"  Exploitation rate: {exploration_metrics.exploitation_rate:.2%}")
    print(f"  Total trials: {exploration_metrics.total_trials}")
    print(f"  Convergence score: {exploration_metrics.convergence_score:.2f}")
    
    # Show safety summary
    print("\n12. Safety and constraints summary:")
    safety_summary = system.safety_manager.get_safety_summary()
    print(f"  Deterministic level: {safety_summary['deterministic_level']}")
    print(f"  Active constraints: {safety_summary['active_constraints']}")
    print(f"  Recent violations: {safety_summary['recent_violations']}")
    print(f"  Operations executed: {safety_summary['operations_executed']}")
    
    print("\n=== Example completed successfully! ===")
    print("\nKey takeaways:")
    print("• System automatically allocated budget based on expected payoff")
    print("• Performance updates influenced subsequent allocation decisions")
    print("• Safety constraints ensured deterministic and safe behavior")
    print("• Multiple report types provide different levels of detail")
    print("• Exploration vs exploitation balance was maintained throughout")
    
    # Show final asset summary
    print(f"\nFinal asset summary:")
    print(f"{'Asset':<25} {'Budget':<12} {'Spent':<10} {'Performance':<12} {'Confidence':<11}")
    print("-" * 75)
    for asset in system.assets.values():
        print(f"{asset.name:<25} ${asset.current_budget:<11,.0f} ${asset.total_spent:<9,.0f} {asset.expected_payoff:<11.3f} {asset.confidence_score:<10.2f}")

if __name__ == "__main__":
    main()
