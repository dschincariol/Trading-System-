"""
Safety Constraints and Deterministic Behavior

Comprehensive safety system ensuring deterministic behavior, preventing catastrophic failures,
and maintaining system stability under all conditions.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional, Any, Union
from dataclasses import dataclass, field
from enum import Enum
import logging
from datetime import datetime, timedelta
import hashlib
import json
from abc import ABC, abstractmethod
from research_budget_allocator import ResearchAsset, AllocationDecision

logger = logging.getLogger(__name__)

class ConstraintType(Enum):
    BUDGET_LIMIT = "budget_limit"
    ALLOCATION_CAP = "allocation_cap"
    MINIMUM_ALLOCATION = "minimum_allocation"
    CONCENTRATION_RISK = "concentration_risk"
    VELOCITY_LIMIT = "velocity_limit"
    CORRELATION_LIMIT = "correlation_limit"
    RESOURCE_CAP = "resource_cap"
    TIME_LIMIT = "time_limit"
    CUSTOM = "custom"

class ConstraintSeverity(Enum):
    ADVISORY = "advisory"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

class DeterministicLevel(Enum):
    FULL = "full"           # Completely deterministic
    PREDICTABLE = "predictable"  # Predictable with bounded randomness
    BOUNDED = "bounded"     # Bounded randomness with guarantees
    FLEXIBLE = "flexible"   # Some flexibility allowed

@dataclass
class SafetyConstraint:
    """Represents a safety constraint with validation logic"""
    name: str
    constraint_type: ConstraintType
    severity: ConstraintSeverity
    parameters: Dict[str, Any]
    enabled: bool = True
    description: str = ""
    
@dataclass
class ConstraintViolation:
    """Represents a constraint violation"""
    constraint_name: str
    constraint_type: ConstraintType
    severity: ConstraintSeverity
    asset_id: Optional[str]
    value: float
    threshold: float
    message: str
    timestamp: datetime = field(default_factory=datetime.now)
    suggested_action: str = ""

@dataclass
class DeterministicResult:
    """Result of deterministic operation with audit trail"""
    operation: str
    input_hash: str
    output_hash: str
    deterministic_level: DeterministicLevel
    execution_time: float
    success: bool
    audit_trail: List[str] = field(default_factory=list)
    violations: List[ConstraintViolation] = field(default_factory=list)

class ConstraintValidator(ABC):
    """Abstract base class for constraint validators"""
    
    @abstractmethod
    def validate(self, constraint: SafetyConstraint, assets: Dict[str, ResearchAsset], 
                context: Dict[str, Any]) -> List[ConstraintViolation]:
        """Validate constraint and return any violations"""
        pass

class BudgetLimitValidator(ConstraintValidator):
    """Validator for budget limit constraints"""
    
    def validate(self, constraint: SafetyConstraint, assets: Dict[str, ResearchAsset], 
                context: Dict[str, Any]) -> List[ConstraintViolation]:
        violations = []
        
        max_total_budget = constraint.parameters.get("max_total_budget", float('inf'))
        max_asset_budget = constraint.parameters.get("max_asset_budget", float('inf'))
        
        # Check total budget
        total_budget = sum(asset.current_budget for asset in assets.values())
        if total_budget > max_total_budget:
            violations.append(ConstraintViolation(
                constraint_name=constraint.name,
                constraint_type=constraint.constraint_type,
                severity=constraint.severity,
                asset_id=None,
                value=total_budget,
                threshold=max_total_budget,
                message=f"Total budget ${total_budget:,.2f} exceeds limit ${max_total_budget:,.2f}",
                suggested_action="Reduce allocations or increase budget limit"
            ))
        
        # Check individual asset budgets
        for asset_id, asset in assets.items():
            if asset.current_budget > max_asset_budget:
                violations.append(ConstraintViolation(
                    constraint_name=constraint.name,
                    constraint_type=constraint.constraint_type,
                    severity=constraint.severity,
                    asset_id=asset_id,
                    value=asset.current_budget,
                    threshold=max_asset_budget,
                    message=f"Asset {asset.name} budget ${asset.current_budget:,.2f} exceeds limit ${max_asset_budget:,.2f}",
                    suggested_action=f"Reduce {asset.name} allocation"
                ))
        
        return violations

class AllocationCapValidator(ConstraintValidator):
    """Validator for allocation percentage caps"""
    
    def validate(self, constraint: SafetyConstraint, assets: Dict[str, ResearchAsset], 
                context: Dict[str, Any]) -> List[ConstraintViolation]:
        violations = []
        
        max_percentage = constraint.parameters.get("max_percentage", 0.4)  # Default 40%
        total_budget = sum(asset.current_budget for asset in assets.values())
        
        if total_budget <= 0:
            return violations
        
        for asset_id, asset in assets.items():
            percentage = asset.current_budget / total_budget
            if percentage > max_percentage:
                violations.append(ConstraintViolation(
                    constraint_name=constraint.name,
                    constraint_type=constraint.constraint_type,
                    severity=constraint.severity,
                    asset_id=asset_id,
                    value=percentage,
                    threshold=max_percentage,
                    message=f"Asset {asset.name} allocation {percentage:.1%} exceeds cap {max_percentage:.1%}",
                    suggested_action=f"Reduce {asset.name} allocation to under {max_percentage:.1%}"
                ))
        
        return violations

class ConcentrationRiskValidator(ConstraintValidator):
    """Validator for concentration risk (too much in few assets)"""
    
    def validate(self, constraint: SafetyConstraint, assets: Dict[str, ResearchAsset], 
                context: Dict[str, Any]) -> List[ConstraintViolation]:
        violations = []
        
        max_top_assets = constraint.parameters.get("max_top_assets", 3)
        max_top_percentage = constraint.parameters.get("max_top_percentage", 0.7)  # 70% in top 3
        
        total_budget = sum(asset.current_budget for asset in assets.values())
        if total_budget <= 0:
            return violations
        
        # Sort assets by budget
        sorted_assets = sorted(assets.values(), key=lambda a: a.current_budget, reverse=True)
        
        # Check concentration in top assets
        top_budget = sum(asset.current_budget for asset in sorted_assets[:max_top_assets])
        top_percentage = top_budget / total_budget
        
        if top_percentage > max_top_percentage:
            violations.append(ConstraintViolation(
                constraint_name=constraint.name,
                constraint_type=constraint.constraint_type,
                severity=constraint.severity,
                asset_id=None,
                value=top_percentage,
                threshold=max_top_percentage,
                message=f"Top {max_top_assets} assets control {top_percentage:.1%} of budget (limit: {max_top_percentage:.1%})",
                suggested_action=f"Diversify allocation away from top {max_top_assets} assets"
            ))
        
        return violations

class VelocityLimitValidator(ConstraintValidator):
    """Validator for allocation velocity limits"""
    
    def validate(self, constraint: SafetyConstraint, assets: Dict[str, ResearchAsset], 
                context: Dict[str, Any]) -> List[ConstraintViolation]:
        violations = []
        
        max_change_percentage = constraint.parameters.get("max_change_percentage", 0.3)  # 30%
        time_window_hours = constraint.parameters.get("time_window_hours", 24)
        
        # Get previous allocations from context
        previous_allocations = context.get("previous_allocations", {})
        current_time = datetime.now()
        
        for asset_id, asset in assets.items():
            if asset_id in previous_allocations:
                prev_budget = previous_allocations[asset_id]
                change_percentage = abs(asset.current_budget - prev_budget) / (prev_budget + 1e-6)
                
                if change_percentage > max_change_percentage:
                    violations.append(ConstraintViolation(
                        constraint_name=constraint.name,
                        constraint_type=constraint.constraint_type,
                        severity=constraint.severity,
                        asset_id=asset_id,
                        value=change_percentage,
                        threshold=max_change_percentage,
                        message=f"Asset {asset.name} changed {change_percentage:.1%} (limit: {max_change_percentage:.1%})",
                        suggested_action=f"Limit changes to under {max_change_percentage:.1%} per {time_window_hours}h"
                    ))
        
        return violations

class ResourceCapValidator(ConstraintValidator):
    """Validator for resource usage caps"""
    
    def validate(self, constraint: SafetyConstraint, assets: Dict[str, ResearchAsset], 
                context: Dict[str, Any]) -> List[ConstraintViolation]:
        violations = []
        
        max_compute_hours = constraint.parameters.get("max_compute_hours", float('inf'))
        max_data_processed = constraint.parameters.get("max_data_processed", float('inf'))
        
        for asset_id, asset in assets.items():
            # Compute hours check
            if asset.compute_hours_used > max_compute_hours:
                violations.append(ConstraintViolation(
                    constraint_name=constraint.name,
                    constraint_type=constraint.constraint_type,
                    severity=constraint.severity,
                    asset_id=asset_id,
                    value=asset.compute_hours_used,
                    threshold=max_compute_hours,
                    message=f"Asset {asset.name} used {asset.compute_hours_used:.0f} compute hours (limit: {max_compute_hours:.0f})",
                    suggested_action=f"Stop or throttle {asset.name} compute usage"
                ))
            
            # Data processed check
            if asset.data_processed > max_data_processed:
                violations.append(ConstraintViolation(
                    constraint_name=constraint.name,
                    constraint_type=constraint.constraint_type,
                    severity=constraint.severity,
                    asset_id=asset_id,
                    value=asset.data_processed,
                    threshold=max_data_processed,
                    message=f"Asset {asset.name} processed {asset.data_processed:.0f} data units (limit: {max_data_processed:.0f})",
                    suggested_action=f"Limit {asset.name} data processing"
                ))
        
        return violations

class SafetyConstraintsManager:
    """Main safety constraints management system"""
    
    def __init__(self, deterministic_level: DeterministicLevel = DeterministicLevel.FULL):
        self.deterministic_level = deterministic_level
        self.constraints: Dict[str, SafetyConstraint] = {}
        self.validators: Dict[ConstraintType, ConstraintValidator] = {}
        self.violations: List[ConstraintViolation] = []
        self.audit_trail: List[DeterministicResult] = []
        
        # Deterministic behavior controls
        self.random_seed = 42
        self.deterministic_cache: Dict[str, Any] = {}
        self.operation_counter = 0
        
        # Initialize validators
        self._initialize_validators()
        
        # Initialize default constraints
        self._initialize_default_constraints()
        
        logger.info(f"Initialized SafetyConstraintsManager with {deterministic_level.value} determinism")
    
    def _initialize_validators(self):
        """Initialize constraint validators"""
        self.validators = {
            ConstraintType.BUDGET_LIMIT: BudgetLimitValidator(),
            ConstraintType.ALLOCATION_CAP: AllocationCapValidator(),
            ConstraintType.CONCENTRATION_RISK: ConcentrationRiskValidator(),
            ConstraintType.VELOCITY_LIMIT: VelocityLimitValidator(),
            ConstraintType.RESOURCE_CAP: ResourceCapValidator(),
        }
    
    def _initialize_default_constraints(self):
        """Initialize default safety constraints"""
        self.constraints = {
            "total_budget_limit": SafetyConstraint(
                name="Total Budget Limit",
                constraint_type=ConstraintType.BUDGET_LIMIT,
                severity=ConstraintSeverity.CRITICAL,
                parameters={"max_total_budget": 10000000},  # $10M default
                description="Prevent total budget from exceeding limit"
            ),
            "individual_asset_cap": SafetyConstraint(
                name="Individual Asset Cap",
                constraint_type=ConstraintType.ALLOCATION_CAP,
                severity=ConstraintSeverity.ERROR,
                parameters={"max_percentage": 0.4},  # 40% max per asset
                description="Prevent any single asset from dominating budget"
            ),
            "concentration_risk": SafetyConstraint(
                name="Concentration Risk",
                constraint_type=ConstraintType.CONCENTRATION_RISK,
                severity=ConstraintSeverity.WARNING,
                parameters={"max_top_assets": 3, "max_top_percentage": 0.7},  # 70% in top 3
                description="Prevent excessive concentration in few assets"
            ),
            "velocity_limit": SafetyConstraint(
                name="Allocation Velocity Limit",
                constraint_type=ConstraintType.VELOCITY_LIMIT,
                severity=ConstraintSeverity.WARNING,
                parameters={"max_change_percentage": 0.3, "time_window_hours": 24},
                description="Limit rapid allocation changes"
            ),
            "resource_cap": SafetyConstraint(
                name="Resource Usage Cap",
                constraint_type=ConstraintType.RESOURCE_CAP,
                severity=ConstraintSeverity.ERROR,
                parameters={"max_compute_hours": 100000, "max_data_processed": 1000000},
                description="Prevent excessive resource consumption"
            )
        }
    
    def add_constraint(self, constraint: SafetyConstraint):
        """Add a new safety constraint"""
        self.constraints[constraint.name] = constraint
        logger.info(f"Added constraint: {constraint.name}")
    
    def remove_constraint(self, name: str) -> bool:
        """Remove a safety constraint"""
        if name in self.constraints:
            del self.constraints[name]
            logger.info(f"Removed constraint: {name}")
            return True
        return False
    
    def enable_constraint(self, name: str, enabled: bool = True):
        """Enable or disable a constraint"""
        if name in self.constraints:
            self.constraints[name].enabled = enabled
            logger.info(f"Constraint {name} {'enabled' if enabled else 'disabled'}")
        else:
            logger.warning(f"Constraint {name} not found")
    
    def update_constraint_parameters(self, name: str, parameters: Dict[str, Any]):
        """Update constraint parameters"""
        if name in self.constraints:
            self.constraints[name].parameters.update(parameters)
            logger.info(f"Updated parameters for constraint {name}")
        else:
            logger.warning(f"Constraint {name} not found")
    
    def validate_constraints(self, assets: Dict[str, ResearchAsset], 
                           context: Optional[Dict[str, Any]] = None) -> List[ConstraintViolation]:
        """Validate all enabled constraints"""
        if context is None:
            context = {}
        
        violations = []
        
        for constraint_name, constraint in self.constraints.items():
            if not constraint.enabled:
                continue
            
            validator = self.validators.get(constraint.constraint_type)
            if validator is None:
                logger.warning(f"No validator for constraint type {constraint.constraint_type.value}")
                continue
            
            try:
                constraint_violations = validator.validate(constraint, assets, context)
                violations.extend(constraint_violations)
            except Exception as e:
                logger.error(f"Error validating constraint {constraint_name}: {e}")
        
        # Store violations
        self.violations.extend(violations)
        
        # Keep only recent violations
        if len(self.violations) > 1000:
            self.violations = self.violations[-500:]
        
        return violations
    
    def enforce_deterministic_behavior(self, operation: str, func: callable, *args, **kwargs) -> DeterministicResult:
        """Execute function with deterministic behavior guarantees"""
        start_time = datetime.now()
        
        # Create deterministic input hash
        input_data = {
            "operation": operation,
            "args": args,
            "kwargs": kwargs,
            "seed": self.random_seed,
            "counter": self.operation_counter
        }
        input_hash = self._create_hash(input_data)
        
        audit_trail = [f"Starting operation: {operation}", f"Input hash: {input_hash[:16]}..."]
        
        try:
            # Check cache for deterministic results
            if self.deterministic_level == DeterministicLevel.FULL and input_hash in self.deterministic_cache:
                result = self.deterministic_cache[input_hash]
                audit_trail.append("Result retrieved from deterministic cache")
                execution_time = (datetime.now() - start_time).total_seconds()
                
                deterministic_result = DeterministicResult(
                    operation=operation,
                    input_hash=input_hash,
                    output_hash=self._create_hash(result),
                    deterministic_level=self.deterministic_level,
                    execution_time=execution_time,
                    success=True,
                    audit_trail=audit_trail
                )
                
                self.audit_trail.append(deterministic_result)
                return deterministic_result
            
            # Set deterministic random seed if needed
            if self.deterministic_level in [DeterministicLevel.FULL, DeterministicLevel.PREDICTABLE]:
                np.random.seed(self.random_seed + self.operation_counter)
                audit_trail.append(f"Set random seed to {self.random_seed + self.operation_counter}")
            
            # Execute the function
            result = func(*args, **kwargs)
            
            # Create output hash
            output_hash = self._create_hash(result)
            audit_trail.append(f"Output hash: {output_hash[:16]}...")
            
            # Cache result for full determinism
            if self.deterministic_level == DeterministicLevel.FULL:
                self.deterministic_cache[input_hash] = result
                audit_trail.append("Result cached for deterministic behavior")
            
            execution_time = (datetime.now() - start_time).total_seconds()
            
            deterministic_result = DeterministicResult(
                operation=operation,
                input_hash=input_hash,
                output_hash=output_hash,
                deterministic_level=self.deterministic_level,
                execution_time=execution_time,
                success=True,
                audit_trail=audit_trail
            )
            
        except Exception as e:
            execution_time = (datetime.now() - start_time).total_seconds()
            audit_trail.append(f"Error: {str(e)}")
            
            deterministic_result = DeterministicResult(
                operation=operation,
                input_hash=input_hash,
                output_hash="",
                deterministic_level=self.deterministic_level,
                execution_time=execution_time,
                success=False,
                audit_trail=audit_trail
            )
            
            logger.error(f"Deterministic operation failed: {e}")
        
        # Update operation counter
        self.operation_counter += 1
        
        # Store in audit trail
        self.audit_trail.append(deterministic_result)
        
        # Keep audit trail manageable
        if len(self.audit_trail) > 1000:
            self.audit_trail = self.audit_trail[-500:]
        
        return deterministic_result
    
    def _create_hash(self, data: Any) -> str:
        """Create deterministic hash from data"""
        try:
            # Convert to JSON string for consistent serialization
            json_str = json.dumps(data, sort_keys=True, default=str)
            return hashlib.sha256(json_str.encode()).hexdigest()
        except (TypeError, ValueError):
            # Fallback to string representation
            return hashlib.sha256(str(data).encode()).hexdigest()
    
    def safe_allocation(self, assets: Dict[str, ResearchAsset], 
                       allocation_decisions: List[AllocationDecision]) -> Tuple[List[AllocationDecision], List[ConstraintViolation]]:
        """Apply safety constraints to allocation decisions"""
        # Create temporary assets with proposed allocations
        temp_assets = {}
        for asset_id, asset in assets.items():
            temp_asset = ResearchAsset(
                id=asset.id,
                name=asset.name,
                category=asset.category,
                initial_budget=asset.initial_budget,
                current_budget=asset.current_budget,
                total_spent=asset.total_spent,
                status=asset.status,
                start_time=asset.start_time,
                last_update=asset.last_update,
                expected_payoff=asset.expected_payoff,
                confidence_score=asset.confidence_score,
                recent_performance=asset.recent_performance.copy(),
                exploration_score=asset.exploration_score,
                compute_hours_used=asset.compute_hours_used,
                data_processed=asset.data_processed
            )
            temp_assets[asset_id] = temp_asset
        
        # Apply proposed allocations
        for decision in allocation_decisions:
            if decision.asset_id in temp_assets:
                temp_assets[decision.asset_id].current_budget += decision.allocated_amount
        
        # Validate constraints
        context = {"previous_allocations": {aid: a.current_budget for aid, a in assets.items()}}
        violations = self.validate_constraints(temp_assets, context)
        
        # Filter out critical and error violations
        critical_violations = [v for v in violations if v.severity in [ConstraintSeverity.CRITICAL, ConstraintSeverity.ERROR]]
        
        if critical_violations:
            logger.warning(f"Critical constraint violations detected: {len(critical_violations)}")
            
            # Adjust allocations to satisfy constraints
            safe_decisions = self._adjust_allocations_for_safety(allocation_decisions, critical_violations, assets)
            return safe_decisions, violations
        else:
            return allocation_decisions, violations
    
    def _adjust_allocations_for_safety(self, decisions: List[AllocationDecision], 
                                     violations: List[ConstraintViolation], 
                                     assets: Dict[str, ResearchAsset]) -> List[AllocationDecision]:
        """Adjust allocation decisions to satisfy safety constraints"""
        safe_decisions = decisions.copy()
        
        # Group violations by type
        budget_violations = [v for v in violations if v.constraint_type == ConstraintType.BUDGET_LIMIT]
        cap_violations = [v for v in violations if v.constraint_type == ConstraintType.ALLOCATION_CAP]
        concentration_violations = [v for v in violations if v.constraint_type == ConstraintType.CONCENTRATION_RISK]
        
        # Handle budget limit violations
        for violation in budget_violations:
            if violation.asset_id:
                # Reduce allocation for violating asset
                for decision in safe_decisions:
                    if decision.asset_id == violation.asset_id:
                        reduction = decision.allocated_amount * 0.5  # Reduce by 50%
                        decision.allocated_amount -= reduction
                        decision.reasoning += f" [Reduced by ${reduction:,.2f} due to budget limit]"
        
        # Handle allocation cap violations
        for violation in cap_violations:
            if violation.asset_id:
                # Scale down allocation to meet cap
                for decision in safe_decisions:
                    if decision.asset_id == violation.asset_id:
                        max_allowed = violation.threshold * sum(a.current_budget for a in assets.values())
                        if decision.allocated_amount > max_allowed:
                            decision.allocated_amount = max_allowed
                            decision.reasoning += f" [Capped at ${max_allowed:,.2f} due to allocation cap]"
        
        # Handle concentration risk violations
        if concentration_violations:
            # Redistribute from top assets to others
            total_budget = sum(a.current_budget for a in assets.values())
            safe_decisions = self._redistribute_for_diversification(safe_decisions, assets, total_budget)
        
        return safe_decisions
    
    def _redistribute_for_diversification(self, decisions: List[AllocationDecision], 
                                        assets: Dict[str, ResearchAsset], 
                                        total_budget: float) -> List[AllocationDecision]:
        """Redistribute allocations to improve diversification"""
        # Sort decisions by allocation amount (largest first)
        sorted_decisions = sorted(decisions, key=lambda d: d.allocated_amount, reverse=True)
        
        # Reduce top 3 allocations and redistribute to others
        top_decisions = sorted_decisions[:3]
        other_decisions = sorted_decisions[3:]
        
        if not other_decisions:
            return decisions
        
        # Calculate reduction amount (20% of top allocations)
        total_reduction = sum(d.allocated_amount * 0.2 for d in top_decisions)
        
        # Reduce top allocations
        for decision in top_decisions:
            reduction = decision.allocated_amount * 0.2
            decision.allocated_amount -= reduction
            decision.reasoning += f" [Reduced by ${reduction:,.2f} for diversification]"
        
        # Redistribute to other assets
        redistribution_per_asset = total_reduction / len(other_decisions)
        
        for decision in other_decisions:
            decision.allocated_amount += redistribution_per_asset
            decision.reasoning += f" [Increased by ${redistribution_per_asset:,.2f} for diversification]"
        
        return decisions
    
    def get_safety_summary(self) -> Dict:
        """Get comprehensive safety summary"""
        active_constraints = len([c for c in self.constraints.values() if c.enabled])
        recent_violations = [v for v in self.violations if v.timestamp > datetime.now() - timedelta(hours=24)]
        
        severity_counts = {}
        for violation in recent_violations:
            severity = violation.severity.value
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
        
        return {
            "deterministic_level": self.deterministic_level.value,
            "active_constraints": active_constraints,
            "total_constraints": len(self.constraints),
            "recent_violations": len(recent_violations),
            "severity_breakdown": severity_counts,
            "operations_executed": len(self.audit_trail),
            "cache_size": len(self.deterministic_cache)
        }
    
    def export_safety_report(self, filename: Optional[str] = None) -> str:
        """Export detailed safety report"""
        if filename is None:
            filename = f"safety_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        report = {
            "timestamp": datetime.now().isoformat(),
            "summary": self.get_safety_summary(),
            "constraints": [
                {
                    "name": c.name,
                    "type": c.constraint_type.value,
                    "severity": c.severity.value,
                    "enabled": c.enabled,
                    "parameters": c.parameters,
                    "description": c.description
                }
                for c in self.constraints.values()
            ],
            "recent_violations": [
                {
                    "constraint_name": v.constraint_name,
                    "type": v.constraint_type.value,
                    "severity": v.severity.value,
                    "asset_id": v.asset_id,
                    "value": v.value,
                    "threshold": v.threshold,
                    "message": v.message,
                    "suggested_action": v.suggested_action,
                    "timestamp": v.timestamp.isoformat()
                }
                for v in self.violations[-50:]  # Last 50 violations
            ],
            "audit_trail": [
                {
                    "operation": a.operation,
                    "deterministic_level": a.deterministic_level.value,
                    "execution_time": a.execution_time,
                    "success": a.success,
                    "timestamp": a.timestamp.isoformat()
                }
                for a in self.audit_trail[-20:]  # Last 20 operations
            ]
        }
        
        with open(filename, 'w') as f:
            json.dump(report, f, indent=2)
        
        logger.info(f"Safety report exported to {filename}")
        return filename

# Example usage
if __name__ == "__main__":
    from research_budget_allocator import ResearchAsset, AllocationDecision
    
    # Create safety manager
    safety_manager = SafetyConstraintsManager(deterministic_level=DeterministicLevel.FULL)
    
    # Create test assets
    assets = {
        "asset1": ResearchAsset("asset1", "Large Asset", "ml", 100000, 80000),
        "asset2": ResearchAsset("asset2", "Medium Asset", "cv", 100000, 50000),
        "asset3": ResearchAsset("asset3", "Small Asset", "nlp", 100000, 20000),
    }
    
    # Create allocation decisions (some that violate constraints)
    decisions = [
        AllocationDecision("asset1", 50000, "Large allocation", 0.8, 0.2, 0.1),
        AllocationDecision("asset2", 30000, "Medium allocation", 0.6, 0.3, 0.2),
        AllocationDecision("asset3", 10000, "Small allocation", 0.4, 0.4, 0.3),
    ]
    
    # Apply safety constraints
    safe_decisions, violations = safety_manager.safe_allocation(assets, decisions)
    
    print(f"Original decisions: {len(decisions)}")
    print(f"Safe decisions: {len(safe_decisions)}")
    print(f"Violations: {len(violations)}")
    
    for violation in violations:
        print(f"  {violation.severity.value}: {violation.message}")
    
    # Test deterministic behavior
    def test_function(x, y):
        return np.random.random() + x + y
    
    result1 = safety_manager.enforce_deterministic_behavior("test", test_function, 1, 2)
    result2 = safety_manager.enforce_deterministic_behavior("test", test_function, 1, 2)
    
    print(f"\nDeterministic test:")
    print(f"Result 1: {result1.success}, hash: {result1.output_hash[:16]}...")
    print(f"Result 2: {result2.success}, hash: {result2.output_hash[:16]}...")
    print(f"Same result: {result1.output_hash == result2.output_hash}")
    
    # Get safety summary
    summary = safety_manager.get_safety_summary()
    print(f"\nSafety summary:")
    for key, value in summary.items():
        print(f"  {key}: {value}")
    
    # Export report
    report_file = safety_manager.export_safety_report()
    print(f"\nSafety report exported to: {report_file}")
