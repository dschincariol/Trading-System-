"""
Research Budget Allocation System - Main Integration

Complete integration of all components into a cohesive research budget allocation system.
This is the main entry point that orchestrates all the subsystems.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional, Any
import json
from pathlib import Path

# Import all system components
from research_budget_allocator import ResearchBudgetAllocator, ResearchAsset, AllocationDecision, AllocationStrategy
from exploration_exploitation_manager import ExplorationExploitationManager, ExplorationStrategy, ExplorationMetrics
from stop_criteria_manager import StopCriteriaManager, StopDecision
from budget_reallocator import BudgetReallocator, ReallocationEvent, ReallocationStrategy
from feedback_monitor import FeedbackMonitor, SignalType, AlertLevel
from safety_constraints import SafetyConstraintsManager, DeterministicLevel, ConstraintViolation
from explainable_reporting import ExplainableReportingSystem, ReportType, Explanation

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('research_allocator.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class ResearchBudgetAllocationSystem:
    """Main system that integrates all components"""
    
    def __init__(self, total_budget: float, config: Optional[Dict[str, Any]] = None):
        """
        Initialize the complete research budget allocation system
        
        Args:
            total_budget: Total research budget to allocate
            config: Configuration dictionary for system parameters
        """
        self.total_budget = total_budget
        self.config = config or self._default_config()
        
        # Initialize all components
        self.allocator = ResearchBudgetAllocator(
            total_budget=total_budget,
            allocation_strategy=AllocationStrategy[self.config['allocation_strategy']]
        )
        
        self.exploration_manager = ExplorationExploitationManager(
            strategy=ExplorationStrategy[self.config['exploration_strategy']],
            **self.config['exploration_params']
        )
        
        self.stop_manager = StopCriteriaManager()
        self.reallocator = BudgetReallocator(
            reallocation_strategy=ReallocationStrategy[self.config['reallocation_strategy']],
            **self.config['reallocation_params']
        )
        
        self.monitor = FeedbackMonitor(
            window_size=self.config['monitoring_window_size'],
            alert_threshold=self.config['alert_threshold']
        )
        
        self.safety_manager = SafetyConstraintsManager(
            deterministic_level=DeterministicLevel[self.config['deterministic_level']]
        )
        
        self.reporting_system = ExplainableReportingSystem(
            output_dir=self.config['reports_output_dir']
        )
        
        # System state
        self.assets: Dict[str, ResearchAsset] = {}
        self.allocation_history: List[AllocationDecision] = []
        self.explanations: List[Explanation] = []
        
        logger.info(f"Initialized Research Budget Allocation System with ${total_budget:,.2f} budget")
    
    def _default_config(self) -> Dict[str, Any]:
        """Default system configuration"""
        return {
            'allocation_strategy': 'BALANCED',
            'exploration_strategy': 'ADAPTIVE_UCB',
            'exploration_params': {
                'initial_c': 2.0,
                'adaptation_rate': 0.1
            },
            'reallocation_strategy': 'PORTFOLIO_OPTIMIZATION',
            'reallocation_params': {
                'min_reallocation_amount': 1000,
                'max_reallocation_frequency': 7
            },
            'monitoring_window_size': 100,
            'alert_threshold': 2.0,
            'deterministic_level': 'FULL',
            'reports_output_dir': 'reports',
            'auto_reallocate': True,
            'auto_stop': True,
            'reporting_frequency': 'daily'
        }
    
    def add_research_asset(self, asset_id: str, name: str, category: str, 
                          initial_budget: float, **kwargs) -> bool:
        """
        Add a new research asset to the system
        
        Args:
            asset_id: Unique identifier for the asset
            name: Human-readable name
            category: Category of research (e.g., 'ml', 'cv', 'nlp')
            initial_budget: Initial budget allocation
            **kwargs: Additional asset parameters
        
        Returns:
            True if asset was added successfully
        """
        try:
            asset = ResearchAsset(
                id=asset_id,
                name=name,
                category=category,
                initial_budget=initial_budget,
                current_budget=0,  # Will be set during allocation
                **kwargs
            )
            
            self.allocator.add_asset(asset)
            self.assets[asset_id] = asset
            
            logger.info(f"Added research asset: {name} ({asset_id}) with ${initial_budget:,.2f} budget")
            return True
            
        except Exception as e:
            logger.error(f"Failed to add asset {asset_id}: {e}")
            return False
    
    def run_allocation_cycle(self) -> Dict[str, Any]:
        """
        Run a complete allocation cycle
        
        This is the main method that orchestrates the entire allocation process:
        1. Check stop criteria
        2. Calculate exploration scores
        3. Allocate budget
        4. Apply safety constraints
        5. Monitor and record feedback
        6. Check for reallocation needs
        
        Returns:
            Dictionary with cycle results and statistics
        """
        cycle_start = datetime.now()
        logger.info("Starting allocation cycle")
        
        try:
            # Step 1: Check stop criteria for all assets
            stop_decisions = self._check_stop_criteria()
            
            # Step 2: Calculate exploration scores
            exploration_scores = self._calculate_exploration_scores()
            
            # Step 3: Allocate budget using deterministic behavior
            allocation_decisions = self._allocate_budget_deterministically()
            
            # Step 4: Apply safety constraints
            safe_decisions, violations = self.safety_manager.safe_allocation(
                self.assets, allocation_decisions
            )
            
            # Step 5: Execute allocations
            self._execute_allocations(safe_decisions)
            
            # Step 6: Monitor and record feedback
            self._update_monitoring()
            
            # Step 7: Check for reallocation needs
            reallocation_events = self._check_reallocation_needs(stop_decisions)
            
            # Step 8: Generate explanations for key decisions
            self._generate_decision_explanations(safe_decisions, stop_decisions, reallocation_events)
            
            # Compile cycle results
            cycle_results = {
                'cycle_id': f"cycle_{cycle_start.strftime('%Y%m%d_%H%M%S')}",
                'start_time': cycle_start.isoformat(),
                'end_time': datetime.now().isoformat(),
                'duration_seconds': (datetime.now() - cycle_start).total_seconds(),
                'assets_processed': len(self.assets),
                'active_assets': len([a for a in self.assets.values() if a.status.value == 'active']),
                'allocation_decisions': len(safe_decisions),
                'stop_decisions': len([d for decisions in stop_decisions.values() for d in decisions if d.should_stop]),
                'safety_violations': len(violations),
                'reallocation_events': len(reallocation_events),
                'total_allocated': sum(d.allocated_amount for d in safe_decisions),
                'exploration_metrics': self.exploration_manager.get_metrics().__dict__
            }
            
            logger.info(f"Allocation cycle completed: {cycle_results['allocation_decisions']} decisions, "
                       f"{cycle_results['total_allocated']:,.2f} allocated")
            
            return cycle_results
            
        except Exception as e:
            logger.error(f"Allocation cycle failed: {e}")
            raise
    
    def _check_stop_criteria(self) -> Dict[str, List[StopDecision]]:
        """Check stop criteria for all assets"""
        stop_decisions = {}
        
        for asset_id, asset in self.assets.items():
            should_stop, decisions = self.stop_manager.should_stop_asset(asset)
            stop_decisions[asset_id] = decisions
            
            if should_stop and self.config['auto_stop']:
                asset.status = asset.status.STOPPED
                logger.info(f"Auto-stopped asset {asset.name} based on stop criteria")
        
        return stop_decisions
    
    def _calculate_exploration_scores(self) -> Dict[str, tuple]:
        """Calculate exploration scores for all active assets"""
        active_assets = [asset for asset in self.assets.values() if asset.status.value == 'active']
        return self.exploration_manager.calculate_exploration_scores(active_assets)
    
    def _allocate_budget_deterministically(self) -> List[AllocationDecision]:
        """Allocate budget using deterministic behavior"""
        def allocate_func():
            return self.allocator.allocate_budget()
        
        result = self.safety_manager.enforce_deterministic_behavior(
            "budget_allocation", allocate_func
        )
        
        if result.success:
            return self.allocator.allocation_history[-len(self.assets):]  # Get latest decisions
        else:
            logger.error("Deterministic allocation failed")
            return []
    
    def _execute_allocations(self, decisions: List[AllocationDecision]):
        """Execute allocation decisions"""
        for decision in decisions:
            if decision.asset_id in self.assets:
                asset = self.assets[decision.asset_id]
                asset.current_budget += decision.allocated_amount
                self.allocation_history.append(decision)
        
        logger.info(f"Executed {len(decisions)} allocation decisions")
    
    def _update_monitoring(self):
        """Update monitoring system with current asset states"""
        for asset in self.assets.values():
            self.monitor.process_asset_update(asset)
    
    def _check_reallocation_needs(self, stop_decisions: Dict[str, List[StopDecision]]) -> List[ReallocationEvent]:
        """Check if reallocation is needed and execute if configured"""
        if not self.config['auto_reallocate']:
            return []
        
        should_reallocate, trigger, reasoning = self.reallocator.should_reallocate(
            self.assets, stop_decisions
        )
        
        if should_reallocate:
            # Calculate available budget from stopped assets
            available_budget = sum(
                self.assets[asset_id].current_budget 
                for asset_id, decisions in stop_decisions.items() 
                if any(d.should_stop for d in decisions)
            )
            
            if available_budget > self.reallocator.min_reallocation_amount:
                decisions = self.reallocator.calculate_reallocation(
                    self.assets, trigger, available_budget
                )
                
                event = self.reallocator.execute_reallocation(self.assets, decisions)
                logger.info(f"Executed reallocation: ${event.amount_reallocated:,.2f}")
                return [event]
        
        return []
    
    def _generate_decision_explanations(self, allocation_decisions: List[AllocationDecision],
                                      stop_decisions: Dict[str, List[StopDecision]],
                                      reallocation_events: List[ReallocationEvent]):
        """Generate explanations for key decisions"""
        # Explain allocation decisions
        for decision in allocation_decisions[:5]:  # Explain top 5 decisions
            asset = self.assets.get(decision.asset_id)
            if asset:
                explanation = self.reporting_system.create_decision_explanation(
                    decision_type="budget_allocation",
                    input_data={
                        "asset_performance": asset.expected_payoff,
                        "asset_confidence": asset.confidence_score,
                        "asset_exploration": asset.exploration_score
                    },
                    reasoning=decision.reasoning,
                    confidence=1.0 - self.safety_manager.calculate_risk_adjustment(asset),
                    factors=[
                        ("expected_payoff", asset.expected_payoff, "Performance-based allocation"),
                        ("confidence_score", asset.confidence_score, "Reliability of performance data"),
                        ("exploration_bonus", asset.exploration_score, "Discovery potential")
                    ],
                    alternatives_considered=["equal_allocation", "performance_only", "exploration_only"]
                )
                self.explanations.append(explanation)
        
        # Explain stop decisions
        for asset_id, decisions in stop_decisions.items():
            for decision in decisions:
                if decision.should_stop:
                    explanation = self.reporting_system.create_decision_explanation(
                        decision_type="asset_stop",
                        input_data={"asset_id": asset_id},
                        reasoning=decision.details,
                        confidence=decision.confidence,
                        factors=[(decision.reason.value, 1.0, decision.details)],
                        alternatives_considered=["continue_with_reduced_budget", "pause_temporarily"]
                    )
                    self.explanations.append(explanation)
    
    def record_performance_update(self, asset_id: str, performance_metric: float,
                                 amount_spent: float, compute_hours: float = 0.0,
                                 data_amount: float = 0.0) -> bool:
        """
        Record performance update for an asset
        
        Args:
            asset_id: Asset identifier
            performance_metric: Performance value (0-1)
            amount_spent: Amount spent since last update
            compute_hours: Compute hours used
            data_amount: Data processed
        
        Returns:
            True if update was recorded successfully
        """
        try:
            if asset_id not in self.assets:
                logger.error(f"Asset {asset_id} not found")
                return False
            
            asset = self.assets[asset_id]
            
            # Record expenditure and update performance
            self.allocator.record_expenditure(
                asset_id, amount_spent, performance_metric, compute_hours, data_amount
            )
            
            # Update exploration manager
            self.exploration_manager.update_performance(asset, performance_metric)
            
            # Update monitoring
            self.monitor.process_asset_update(asset)
            
            logger.info(f"Recorded performance for {asset.name}: {performance_metric:.3f}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to record performance for {asset_id}: {e}")
            return False
    
    def generate_system_report(self, report_type: ReportType = ReportType.SUMMARY) -> str:
        """Generate comprehensive system report"""
        try:
            # Get current system state
            exploration_metrics = self.exploration_manager.get_metrics()
            stop_decisions = {aid: self.stop_manager.evaluate_stop_criteria(asset) 
                            for aid, asset in self.assets.items()}
            reallocation_events = self.reallocator.reallocation_history
            constraint_violations = self.safety_manager.violations[-50:]  # Last 50 violations
            
            # Generate report
            report_path = self.reporting_system.generate_comprehensive_report(
                report_type=report_type,
                assets=self.assets,
                allocation_decisions=self.allocation_history[-20:],  # Last 20 decisions
                exploration_metrics=exploration_metrics,
                stop_decisions=stop_decisions,
                reallocation_events=reallocation_events[-10:],  # Last 10 events
                constraint_violations=constraint_violations,
                explanations=self.explanations[-10:]  # Last 10 explanations
            )
            
            # Export HTML version
            html_path = self.reporting_system.export_html_report(report_path)
            
            logger.info(f"Generated {report_type.value} report: {html_path}")
            return html_path
            
        except Exception as e:
            logger.error(f"Failed to generate report: {e}")
            raise
    
    def get_system_status(self) -> Dict[str, Any]:
        """Get comprehensive system status"""
        try:
            # Get component summaries
            allocation_summary = self.allocator.get_allocation_summary()
            monitoring_summary = self.monitor.get_metrics_summary()
            safety_summary = self.safety_manager.get_safety_summary()
            reallocation_summary = self.reallocator.get_reallocation_summary()
            
            # Get active alerts
            active_alerts = self.monitor.get_active_alerts()
            
            status = {
                "timestamp": datetime.now().isoformat(),
                "total_budget": self.total_budget,
                "available_budget": self.allocator.available_budget,
                "total_assets": len(self.assets),
                "active_assets": allocation_summary['active_assets'],
                "stopped_assets": allocation_summary['stopped_assets'],
                "allocation_strategy": self.config['allocation_strategy'],
                "exploration_strategy": self.config['exploration_strategy'],
                "reallocation_strategy": self.config['reallocation_strategy'],
                "deterministic_level": self.config['deterministic_level'],
                "allocation_summary": allocation_summary,
                "monitoring_summary": monitoring_summary,
                "safety_summary": safety_summary,
                "reallocation_summary": reallocation_summary,
                "active_alerts_count": len(active_alerts),
                "critical_alerts": len([a for a in active_alerts if a.level == AlertLevel.CRITICAL]),
                "recent_explanations": len(self.explanations[-10:])
            }
            
            return status
            
        except Exception as e:
            logger.error(f"Failed to get system status: {e}")
            return {"error": str(e)}
    
    def export_system_state(self, filepath: Optional[str] = None) -> str:
        """Export complete system state for backup or analysis"""
        if filepath is None:
            filepath = f"system_state_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        try:
            state = {
                "export_timestamp": datetime.now().isoformat(),
                "config": self.config,
                "total_budget": self.total_budget,
                "assets": {
                    asset_id: {
                        "id": asset.id,
                        "name": asset.name,
                        "category": asset.category,
                        "initial_budget": asset.initial_budget,
                        "current_budget": asset.current_budget,
                        "total_spent": asset.total_spent,
                        "status": asset.status.value,
                        "expected_payoff": asset.expected_payoff,
                        "confidence_score": asset.confidence_score,
                        "exploration_score": asset.exploration_score,
                        "compute_hours_used": asset.compute_hours_used,
                        "data_processed": asset.data_processed,
                        "recent_performance": asset.recent_performance,
                        "start_time": asset.start_time.isoformat(),
                        "last_update": asset.last_update.isoformat()
                    }
                    for asset_id, asset in self.assets.items()
                },
                "allocation_history": [
                    {
                        "asset_id": d.asset_id,
                        "allocated_amount": d.allocated_amount,
                        "reasoning": d.reasoning,
                        "expected_payoff": d.expected_payoff,
                        "exploration_bonus": d.exploration_bonus,
                        "risk_score": d.risk_score,
                        "timestamp": d.timestamp.isoformat()
                    }
                    for d in self.allocation_history[-100:]  # Last 100 decisions
                ],
                "explanations": [
                    {
                        "decision_type": e.decision_type,
                        "reasoning": e.reasoning,
                        "confidence": e.confidence,
                        "timestamp": e.timestamp.isoformat()
                    }
                    for e in self.explanations[-50:]  # Last 50 explanations
                ],
                "system_status": self.get_system_status()
            }
            
            with open(filepath, 'w') as f:
                json.dump(state, f, indent=2)
            
            logger.info(f"System state exported to {filepath}")
            return filepath
            
        except Exception as e:
            logger.error(f"Failed to export system state: {e}")
            raise
    
    def load_system_state(self, filepath: str) -> bool:
        """Load system state from file"""
        try:
            with open(filepath, 'r') as f:
                state = json.load(f)
            
            # Restore configuration
            self.config = state['config']
            self.total_budget = state['total_budget']
            
            # Restore assets
            self.assets = {}
            for asset_data in state['assets'].values():
                asset = ResearchAsset(
                    id=asset_data['id'],
                    name=asset_data['name'],
                    category=asset_data['category'],
                    initial_budget=asset_data['initial_budget'],
                    current_budget=asset_data['current_budget'],
                    total_spent=asset_data['total_spent']
                )
                asset.status = asset_data['status']
                asset.expected_payoff = asset_data['expected_payoff']
                asset.confidence_score = asset_data['confidence_score']
                asset.exploration_score = asset_data['exploration_score']
                asset.compute_hours_used = asset_data['compute_hours_used']
                asset.data_processed = asset_data['data_processed']
                asset.recent_performance = asset_data['recent_performance']
                asset.start_time = datetime.fromisoformat(asset_data['start_time'])
                asset.last_update = datetime.fromisoformat(asset_data['last_update'])
                
                self.assets[asset.id] = asset
                self.allocator.add_asset(asset)
            
            logger.info(f"System state loaded from {filepath}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to load system state: {e}")
            return False

# Example usage
if __name__ == "__main__":
    # Initialize the system
    system = ResearchBudgetAllocationSystem(total_budget=1000000)
    
    # Add research assets
    system.add_research_asset("gpt4_finetune", "GPT-4 Fine-tuning", "language_model", 200000)
    system.add_research_asset("vision_transformer", "Vision Transformer", "computer_vision", 150000)
    system.add_research_asset("rl_agent", "Reinforcement Learning", "rl_agent", 100000)
    system.add_research_asset("data_pipeline", "Data Pipeline Optimization", "infrastructure", 180000)
    
    # Run initial allocation
    print("=== Running Initial Allocation ===")
    cycle_results = system.run_allocation_cycle()
    print(f"Allocated ${cycle_results['total_allocated']:,.2f} across {cycle_results['allocation_decisions']} assets")
    
    # Simulate some performance updates
    print("\n=== Recording Performance Updates ===")
    performance_updates = [
        ("gpt4_finetune", 0.8, 50000, 100, 1000),
        ("vision_transformer", 0.6, 30000, 80, 800),
        ("rl_agent", 0.4, 20000, 120, 1200),
        ("data_pipeline", 0.9, 35000, 60, 600),
    ]
    
    for asset_id, performance, spent, compute, data in performance_updates:
        system.record_performance_update(asset_id, performance, spent, compute, data)
    
    # Run another allocation cycle
    print("\n=== Running Second Allocation Cycle ===")
    cycle_results = system.run_allocation_cycle()
    print(f"Allocated ${cycle_results['total_allocated']:,.2f} across {cycle_results['allocation_decisions']} assets")
    
    # Get system status
    print("\n=== System Status ===")
    status = system.get_system_status()
    print(f"Active assets: {status['active_assets']}")
    print(f"Available budget: ${status['available_budget']:,.2f}")
    print(f"Critical alerts: {status['critical_alerts']}")
    
    # Generate report
    print("\n=== Generating Report ===")
    report_path = system.generate_system_report(ReportType.SUMMARY)
    print(f"Report generated: {report_path}")
    
    # Export system state
    print("\n=== Exporting System State ===")
    state_path = system.export_system_state()
    print(f"System state exported: {state_path}")
