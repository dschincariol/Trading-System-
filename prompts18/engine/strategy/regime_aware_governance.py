"""
Regime-Aware Model Governance Integration

Integrates regime detection with existing model governance and capital allocation systems.
Provides regime-aware promotion/demotion decisions and capital allocation adjustments.
"""

import os
import time
import json
import logging
from typing import Dict, Any, List, Tuple, Optional
from dataclasses import dataclass

from engine.storage import connect
from engine.strategy.model_governance import (
    ModelStage, PromotionDecision, evaluate_promotion_gates,
    evaluate_demotion_triggers
)
from engine.strategy.capital_allocation_engine import AllocationTarget, CapitalConstraints
from engine.strategy.regime_detection_system import (
    get_regime_detection_system, RegimeDetectionResult,
    should_promote_model_regime_aware, should_demote_model_regime_aware,
    get_regime_aware_capital_adjustment
)

logger = logging.getLogger(__name__)


@dataclass
class RegimeAwareGovernanceDecision:
    """Decision from regime-aware governance system"""
    model_name: str
    model_type: str
    current_stage: str
    recommended_action: str
    regime_name: str
    regime_confidence: float
    compatibility_score: float
    risk_multiplier: float
    capital_adjustment: float
    reasoning: List[str]
    governance_metrics: Dict[str, float]
    regime_metrics: Dict[str, float]


class RegimeAwareGovernance:
    """
    Integrates regime detection with model governance for smarter promotion/demotion decisions
    """
    
    def __init__(self):
        self.regime_system = get_regime_detection_system()
        self.governance_enabled = os.environ.get("REGIME_AWARE_GOVERNANCE", "1") == "1"
        
    def evaluate_promotion_with_regime(self, model_name: str, model_type: str, 
                                      governance_metrics: Dict[str, float]) -> RegimeAwareGovernanceDecision:
        """
        Evaluate model promotion with regime awareness
        """
        # Get current regime detection
        regime_detection = self.regime_system.detect_regime()
        
        # Standard governance evaluation
        standard_decision = evaluate_promotion_gates(model_name, governance_metrics)
        
        # Regime-aware evaluation
        can_promote, regime_reason = should_promote_model_regime_aware(model_name, governance_metrics)
        
        # Get regime-specific adjustments
        regime_def = self.regime_system.regime_definitions.get(regime_detection.primary_regime)
        risk_multiplier = regime_def.risk_multiplier if regime_def else 1.0
        capital_adjustment = get_regime_aware_capital_adjustment(model_name)
        
        # Combine decisions
        if not self.governance_enabled:
            # Fallback to standard governance
            recommended_action = standard_decision.value
            reasoning = ["Regime-aware governance disabled", f"Standard decision: {standard_decision.value}"]
        else:
            # Combine standard and regime-aware decisions
            if standard_decision == PromotionDecision.PROMOTE and can_promote:
                recommended_action = "PROMOTE_REGIME_ENHANCED"
                reasoning = [
                    f"Standard gates passed: {standard_decision.value}",
                    f"Regime compatible: {regime_detection.primary_regime}",
                    regime_reason
                ]
            elif standard_decision == PromotionDecision.PROMOTE and not can_promote:
                recommended_action = "HOLD_REGIME_BLOCKED"
                reasoning = [
                    "Standard gates passed but regime blocked",
                    f"Current regime: {regime_detection.primary_regime}",
                    regime_reason
                ]
            elif can_promote and standard_decision != PromotionDecision.PROMOTE:
                recommended_action = "HOLD_GOVERNANCE_BLOCKED"
                reasoning = [
                    "Regime compatible but governance gates failed",
                    f"Standard decision: {standard_decision.value}",
                    f"Regime: {regime_detection.primary_regime}"
                ]
            else:
                recommended_action = "HOLD_BOTH_BLOCKED"
                reasoning = [
                    "Both governance and regime checks failed",
                    f"Standard: {standard_decision.value}",
                    f"Regime: {regime_reason}"
                ]
        
        return RegimeAwareGovernanceDecision(
            model_name=model_name,
            model_type=model_type,
            current_stage="challenger",  # Would be determined from model registry
            recommended_action=recommended_action,
            regime_name=regime_detection.primary_regime,
            regime_confidence=regime_detection.confidence,
            compatibility_score=regime_detection.model_compatibility_scores.get(model_name, 0.0),
            risk_multiplier=risk_multiplier,
            capital_adjustment=capital_adjustment,
            reasoning=reasoning,
            governance_metrics=governance_metrics,
            regime_metrics=regime_detection.risk_metrics
        )
    
    def evaluate_demotion_with_regime(self, model_name: str, model_type: str,
                                    governance_metrics: Dict[str, float]) -> RegimeAwareGovernanceDecision:
        """
        Evaluate model demotion with regime protection
        """
        # Get current regime detection
        regime_detection = self.regime_system.detect_regime()
        
        # Standard governance evaluation
        standard_decision = evaluate_demotion_triggers(model_name, governance_metrics)
        
        # Regime-aware evaluation (protection logic)
        should_demote, regime_reason = should_demote_model_regime_aware(model_name, governance_metrics)
        
        # Get regime-specific adjustments
        regime_def = self.regime_system.regime_definitions.get(regime_detection.primary_regime)
        risk_multiplier = regime_def.risk_multiplier if regime_def else 1.0
        capital_adjustment = get_regime_aware_capital_adjustment(model_name)
        
        # Combine decisions with regime protection
        if not self.governance_enabled:
            recommended_action = standard_decision.value
            reasoning = ["Regime-aware governance disabled", f"Standard decision: {standard_decision.value}"]
        else:
            # Regime protection logic
            if standard_decision == PromotionDecision.DEMOTE and not should_demote:
                # Standard wants to demote but regime protects
                recommended_action = "PROTECTED_BY_REGIME"
                reasoning = [
                    "Standard governance suggests demotion",
                    f"Regime protection: {regime_detection.primary_regime}",
                    regime_reason,
                    "Model protected from demotion due to regime compatibility"
                ]
            elif standard_decision == PromotionDecision.DEMOTE and should_demote:
                # Both agree on demotion
                recommended_action = "DEMOTE_REGIME_CONFIRMED"
                reasoning = [
                    "Both governance and regime agree on demotion",
                    f"Regime: {regime_detection.primary_regime}",
                    regime_reason
                ]
            elif standard_decision != PromotionDecision.DEMOTE and should_demote:
                # Regime suggests demotion but standard doesn't
                recommended_action = "MONITOR_REGIME_CONCERN"
                reasoning = [
                    "Standard governance okay but regime concerned",
                    f"Regime: {regime_detection.primary_regime}",
                    regime_reason,
                    "Increased monitoring recommended"
                ]
            else:
                # Neither suggests demotion
                recommended_action = "HOLD_NO_ACTION"
                reasoning = [
                    "No demotion triggers from governance or regime",
                    f"Standard: {standard_decision.value}",
                    f"Regime: {regime_reason}"
                ]
        
        return RegimeAwareGovernanceDecision(
            model_name=model_name,
            model_type=model_type,
            current_stage="champion",  # Would be determined from model registry
            recommended_action=recommended_action,
            regime_name=regime_detection.primary_regime,
            regime_confidence=regime_detection.confidence,
            compatibility_score=regime_detection.model_compatibility_scores.get(model_name, 0.0),
            risk_multiplier=risk_multiplier,
            capital_adjustment=capital_adjustment,
            reasoning=reasoning,
            governance_metrics=governance_metrics,
            regime_metrics=regime_detection.risk_metrics
        )
    
    def adjust_capital_allocation_with_regime(self, allocation_targets: List[AllocationTarget]) -> List[AllocationTarget]:
        """
        Adjust capital allocation targets based on regime compatibility
        """
        if not self.governance_enabled:
            return allocation_targets
        
        # Get current regime detection
        regime_detection = self.regime_system.detect_regime()
        
        # Adjust each allocation target
        adjusted_targets = []
        for target in allocation_targets:
            # Get regime-aware adjustment
            regime_adjustment = get_regime_aware_capital_adjustment(f"{target.strategy}_{target.asset}")
            
            # Apply adjustment
            adjusted_weight = target.weight * regime_adjustment
            
            # Create adjusted target
            adjusted_target = AllocationTarget(
                strategy=target.strategy,
                asset=target.asset,
                horizon=target.horizon,
                weight=adjusted_weight,
                expected_return=target.expected_return,
                confidence=target.confidence,
                correlation_penalty=target.correlation_penalty,
                execution_capacity_limit=target.execution_capacity_limit,
                performance_score=target.performance_score * regime_adjustment
            )
            
            adjusted_targets.append(adjusted_target)
            
            logger.info(f"Adjusted {target.strategy}_{target.asset} weight: {target.weight:.3f} -> {adjusted_weight:.3f} "
                       f"(regime: {regime_detection.primary_regime}, adjustment: {regime_adjustment:.3f})")
        
        return adjusted_targets
    
    def get_regime_governance_summary(self) -> Dict[str, Any]:
        """
        Get summary of regime-aware governance status
        """
        regime_detection = self.regime_system.detect_regime()
        regime_summary = self.regime_system.get_regime_summary(hours_back=24)
        
        return {
            "current_regime": {
                "name": regime_detection.primary_regime,
                "state": regime_detection.regime_state.value,
                "confidence": regime_detection.confidence,
                "risk_metrics": regime_detection.risk_metrics
            },
            "governance_status": {
                "enabled": self.governance_enabled,
                "regime_definitions_count": len(self.regime_system.regime_definitions),
                "model_profiles_count": len(self.regime_system.model_profiles)
            },
            "regime_summary": regime_summary,
            "recommended_actions": regime_detection.recommended_actions
        }
    
    def update_model_performance_with_regime(self, model_name: str, performance_metrics: Dict[str, float]) -> None:
        """
        Update model performance tracking with regime context
        """
        # Get current regime
        regime_detection = self.regime_system.detect_regime()
        
        # Update performance in regime context
        self.regime_system.update_model_performance_in_regime(
            model_name=model_name,
            regime=regime_detection.primary_regime,
            performance_metrics=performance_metrics
        )
        
        logger.info(f"Updated performance for {model_name} in regime {regime_detection.primary_regime}")


# Global instance
_regime_aware_governance = None

def get_regime_aware_governance() -> RegimeAwareGovernance:
    """Get singleton instance of regime-aware governance"""
    global _regime_aware_governance
    if _regime_aware_governance is None:
        _regime_aware_governance = RegimeAwareGovernance()
    return _regime_aware_governance


# Integration functions for existing systems
def evaluate_model_promotion_regime_aware(model_name: str, model_type: str, 
                                        governance_metrics: Dict[str, float]) -> RegimeAwareGovernanceDecision:
    """
    Integration function for model promotion with regime awareness
    """
    governance = get_regime_aware_governance()
    return governance.evaluate_promotion_with_regime(model_name, model_type, governance_metrics)


def evaluate_model_demotion_regime_aware(model_name: str, model_type: str,
                                      governance_metrics: Dict[str, float]) -> RegimeAwareGovernanceDecision:
    """
    Integration function for model demotion with regime protection
    """
    governance = get_regime_aware_governance()
    return governance.evaluate_demotion_with_regime(model_name, model_type, governance_metrics)


def adjust_capital_allocation_regime_aware(allocation_targets: List[AllocationTarget]) -> List[AllocationTarget]:
    """
    Integration function for capital allocation with regime adjustments
    """
    governance = get_regime_aware_governance()
    return governance.adjust_capital_allocation_with_regime(allocation_targets)


def get_regime_governance_status() -> Dict[str, Any]:
    """
    Integration function to get regime-aware governance status
    """
    governance = get_regime_aware_governance()
    return governance.get_regime_governance_summary()


def update_model_performance_regime_aware(model_name: str, performance_metrics: Dict[str, float]) -> None:
    """
    Integration function to update model performance with regime context
    """
    governance = get_regime_aware_governance()
    governance.update_model_performance_with_regime(model_name, performance_metrics)
