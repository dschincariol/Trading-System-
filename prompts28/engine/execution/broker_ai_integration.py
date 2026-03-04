# engine/execution/broker_ai_integration.py
"""
Broker Adapter AI Integration - Safe Advisory Mode

Integrates Execution AI with existing broker adapters in advisory mode only.
No automatic execution - all AI recommendations require manual approval.
"""

import json
import time
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass

from engine.execution.execution_ai_advisor import (
    request_ai_advisory,
    get_pending_ai_recommendations,
    approve_ai_recommendation,
    reject_ai_recommendation,
    record_ai_execution_outcome,
    AdvisoryRecommendation,
    AdvisoryStatus
)
from engine.execution.execution_ledger import log_submit, log_fill
from engine.execution.execution_ai_model import is_model_trained


@dataclass
class AIEnhancedOrderParams:
    """AI-enhanced order parameters"""
    original_qty: float
    recommended_slice_size: Optional[float] = None
    recommended_limit_offset_bps: Optional[float] = None
    recommended_delay_ms: Optional[int] = None
    ai_recommendation_id: Optional[str] = None
    ai_action: Optional[str] = None
    ai_confidence: Optional[float] = None
    ai_reasoning: Optional[str] = None


class BrokerAIIntegration:
    """Safe AI integration for broker adapters"""
    
    def __init__(self, broker_name: str):
        self.broker_name = broker_name
        self.ai_enabled = True
        self.auto_approve_threshold = 0.8  # High confidence threshold for auto-approval (disabled by default)
        self.require_approval = True  # Safety: always require manual approval
        
    def is_ai_available(self) -> bool:
        """Check if AI is available and trained"""
        return self.ai_enabled and is_model_trained()
    
    def enhance_order_request(
        self,
        client_order_id: str,
        symbol: str,
        qty: float,
        aggressiveness: str,
        alpha_ttl_ms: int = 0,
        alpha_half_life_ms: int = 60000,
        venue: Optional[str] = None,
        instrument_type: Optional[str] = None,
        submit_ts_ms: Optional[int] = None
    ) -> AIEnhancedOrderParams:
        """Enhance order request with AI recommendations"""
        
        enhanced_params = AIEnhancedOrderParams(
            original_qty=qty
        )
        
        if not self.is_ai_available():
            return enhanced_params
        
        try:
            # Request AI advisory
            advisory = request_ai_advisory(
                client_order_id=client_order_id,
                broker=self.broker_name,
                symbol=symbol,
                qty=qty,
                aggressiveness=aggressiveness,
                alpha_ttl_ms=alpha_ttl_ms,
                alpha_half_life_ms=alpha_half_life_ms,
                venue=venue,
                instrument_type=instrument_type,
                submit_ts_ms=submit_ts_ms
            )
            
            if advisory:
                enhanced_params.ai_recommendation_id = advisory.recommendation_id
                enhanced_params.ai_action = advisory.action
                enhanced_params.ai_confidence = advisory.confidence
                enhanced_params.ai_reasoning = advisory.reasoning
                enhanced_params.recommended_slice_size = advisory.slice_size
                enhanced_params.recommended_delay_ms = advisory.delay_ms
                enhanced_params.recommended_limit_offset_bps = advisory.limit_offset_bps
                
        except Exception as e:
            # AI failure should not block order submission
            print(f"AI advisory failed for {client_order_id}: {e}")
        
        return enhanced_params
    
    def should_execute_order(self, enhanced_params: AIEnhancedOrderParams) -> Tuple[bool, str]:
        """Determine if order should be executed based on AI recommendation"""
        
        if not enhanced_params.ai_recommendation_id:
            return True, "No AI recommendation available - proceeding normally"
        
        if not self.is_ai_available():
            return True, "AI not available - proceeding normally"
        
        action = enhanced_params.ai_action
        
        # AI recommends NOT to trade
        if action == "avoid_trading":
            if self.require_approval:
                return False, f"AI recommends avoiding trade: {enhanced_params.ai_reasoning}"
            else:
                # Auto-reject if approval not required (safety net)
                return False, f"AI auto-rejected: {enhanced_params.ai_reasoning}"
        
        # AI recommends delay
        if action == "delay_execution" and enhanced_params.recommended_delay_ms:
            if self.require_approval:
                return False, f"AI recommends delay of {enhanced_params.recommended_delay_ms}ms: {enhanced_params.ai_reasoning}"
            # Could implement auto-delay here if needed
        
        # For other actions, proceed with approval
        return True, f"AI recommendation: {action} - {enhanced_params.ai_reasoning}"
    
    def get_order_modifications(self, enhanced_params: AIEnhancedOrderParams) -> Dict[str, Any]:
        """Get order modifications based on AI recommendation"""
        
        modifications = {}
        
        if enhanced_params.ai_action == "slice_order" and enhanced_params.recommended_slice_size:
            modifications['slice_size'] = enhanced_params.recommended_slice_size
            modifications['reason'] = f"AI slicing: {enhanced_params.ai_reasoning}"
        
        if enhanced_params.ai_action == "cross_spread" and enhanced_params.recommended_limit_offset_bps:
            modifications['limit_offset_bps'] = enhanced_params.recommended_limit_offset_bps
            modifications['reason'] = f"AI cross spread: {enhanced_params.ai_reasoning}"
        
        if enhanced_params.ai_action == "proceed_normal" and enhanced_params.recommended_limit_offset_bps:
            modifications['limit_offset_bps'] = enhanced_params.recommended_limit_offset_bps
            modifications['reason'] = f"AI normal execution: {enhanced_params.ai_reasoning}"
        
        return modifications
    
    def submit_order_with_ai(
        self,
        client_order_id: str,
        symbol: str,
        qty: float,
        order_type: str,
        aggressiveness: str,
        limit_px: Optional[float] = None,
        alpha_ttl_ms: int = 0,
        alpha_half_life_ms: int = 60000,
        venue: Optional[str] = None,
        instrument_type: Optional[str] = None,
        portfolio_orders_id: Optional[int] = None,
        source_alert_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Submit order with AI enhancement (advisory mode)"""
        
        # Get AI enhancement
        enhanced_params = self.enhance_order_request(
            client_order_id, symbol, qty, aggressiveness,
            alpha_ttl_ms, alpha_half_life_ms, venue, instrument_type
        )
        
        # Check if should execute
        should_execute, reason = self.should_execute_order(enhanced_params)
        
        if not should_execute:
            return {
                'status': 'rejected',
                'reason': reason,
                'ai_recommendation_id': enhanced_params.ai_recommendation_id,
                'ai_action': enhanced_params.ai_action,
                'ai_confidence': enhanced_params.ai_confidence
            }
        
        # Get order modifications
        modifications = self.get_order_modifications(enhanced_params)
        
        # Apply modifications
        final_qty = qty
        final_limit_px = limit_px
        
        if 'slice_size' in modifications:
            final_qty = modifications['slice_size']
        
        if 'limit_offset_bps' in modifications and limit_px:
            offset_bps = modifications['limit_offset_bps']
            if qty > 0:  # Buy order
                final_limit_px = limit_px * (1 + offset_bps / 10000)
            else:  # Sell order
                final_limit_px = limit_px * (1 - offset_bps / 10000)
        
        # Log the submission with AI context
        extra = {
            'ai_recommendation_id': enhanced_params.ai_recommendation_id,
            'ai_action': enhanced_params.ai_action,
            'ai_confidence': enhanced_params.ai_confidence,
            'ai_reasoning': enhanced_params.ai_reasoning,
            'ai_modifications': modifications,
            'original_qty': qty,
            'final_qty': final_qty,
            'aggressiveness': aggressiveness,
            'order_type': order_type,
            'alpha_ttl_ms': alpha_ttl_ms,
            'alpha_half_life_ms': alpha_half_life_ms,
            'venue': venue,
            'instrument_type': instrument_type
        }
        
        # Submit to broker (this would call the actual broker adapter)
        # For now, we just log it
        log_submit(
            client_order_id=client_order_id,
            broker=self.broker_name,
            symbol=symbol,
            qty=final_qty,
            submit_ts_ms=int(time.time() * 1000),
            ref_px=final_limit_px,
            portfolio_orders_id=portfolio_orders_id,
            source_alert_id=source_alert_id,
            extra=extra
        )
        
        return {
            'status': 'submitted',
            'client_order_id': client_order_id,
            'final_qty': final_qty,
            'final_limit_px': final_limit_px,
            'ai_recommendation_id': enhanced_params.ai_recommendation_id,
            'ai_modifications': modifications,
            'reason': reason
        }
    
    def record_fill_with_ai(
        self,
        client_order_id: str,
        fill_id: str,
        fill_px: float,
        fill_qty: float,
        fill_ts_ms: int,
        fees: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None
    ) -> None:
        """Record fill with AI outcome tracking"""
        
        # Log the fill normally
        log_fill(
            client_order_id=client_order_id,
            fill_id=fill_id,
            broker=self.broker_name,
            symbol=extra.get('symbol', '') if extra else '',
            qty=fill_qty,
            fill_px=fill_px,
            fill_ts_ms=fill_ts_ms,
            fees=fees,
            extra=extra
        )
        
        # Extract AI recommendation ID from order
        ai_recommendation_id = None
        if extra and 'ai_recommendation_id' in extra:
            ai_recommendation_id = extra['ai_recommendation_id']
        
        if ai_recommendation_id:
            # Calculate actual outcomes
            ref_px = extra.get('ref_px', fill_px) if extra else fill_px
            actual_slippage_bps = 0.0
            if ref_px and ref_px > 0:
                side = 1 if fill_qty > 0 else -1
                actual_slippage_bps = ((fill_px - ref_px) / ref_px) * 10000 * side
            
            submit_ts_ms = extra.get('submit_ts_ms', fill_ts_ms) if extra else fill_ts_ms
            actual_fill_latency_ms = max(0, fill_ts_ms - submit_ts_ms)
            
            actual_cost_bps = abs(actual_slippage_bps)
            if fees:
                actual_cost_bps += abs(fees) / abs(fill_qty * fill_px) * 10000
            
            # Record AI outcome
            try:
                record_ai_execution_outcome(
                    recommendation_id=ai_recommendation_id,
                    actual_slippage_bps=actual_slippage_bps,
                    actual_fill_latency_ms=actual_fill_latency_ms,
                    actual_cost_bps=actual_cost_bps,
                    notes=f"Fill recorded: {fill_qty} @ {fill_px}"
                )
            except Exception as e:
                print(f"Failed to record AI outcome: {e}")
    
    def get_pending_ai_recommendations(self) -> List[AdvisoryRecommendation]:
        """Get pending AI recommendations for this broker"""
        return get_pending_ai_recommendations(self.broker_name)
    
    def approve_ai_recommendation(self, recommendation_id: str, approved_by: str) -> bool:
        """Approve an AI recommendation"""
        return approve_ai_recommendation(recommendation_id, approved_by)
    
    def reject_ai_recommendation(self, recommendation_id: str, rejected_by: str, reason: str) -> bool:
        """Reject an AI recommendation"""
        return reject_ai_recommendation(recommendation_id, rejected_by, reason)


# Broker-specific AI integration instances
_broker_ai_integrations = {}


def get_broker_ai_integration(broker_name: str) -> BrokerAIIntegration:
    """Get or create AI integration for a specific broker"""
    if broker_name not in _broker_ai_integrations:
        _broker_ai_integrations[broker_name] = BrokerAIIntegration(broker_name)
    return _broker_ai_integrations[broker_name]


# Convenience functions for existing broker adapters
def enhance_order_with_ai(
    broker_name: str,
    client_order_id: str,
    symbol: str,
    qty: float,
    aggressiveness: str,
    alpha_ttl_ms: int = 0,
    alpha_half_life_ms: int = 60000,
    venue: Optional[str] = None,
    instrument_type: Optional[str] = None
) -> AIEnhancedOrderParams:
    """Enhance order with AI (called by broker adapters)"""
    integration = get_broker_ai_integration(broker_name)
    return integration.enhance_order_request(
        client_order_id, symbol, qty, aggressiveness,
        alpha_ttl_ms, alpha_half_life_ms, venue, instrument_type
    )


def submit_order_with_ai_safety(
    broker_name: str,
    client_order_id: str,
    symbol: str,
    qty: float,
    order_type: str,
    aggressiveness: str,
    limit_px: Optional[float] = None,
    alpha_ttl_ms: int = 0,
    alpha_half_life_ms: int = 60000,
    venue: Optional[str] = None,
    instrument_type: Optional[str] = None,
    portfolio_orders_id: Optional[int] = None,
    source_alert_id: Optional[int] = None
) -> Dict[str, Any]:
    """Submit order with AI safety checks"""
    integration = get_broker_ai_integration(broker_name)
    return integration.submit_order_with_ai(
        client_order_id, symbol, qty, order_type, aggressiveness, limit_px,
        alpha_ttl_ms, alpha_half_life_ms, venue, instrument_type,
        portfolio_orders_id, source_alert_id
    )


def record_fill_with_ai_tracking(
    broker_name: str,
    client_order_id: str,
    fill_id: str,
    fill_px: float,
    fill_qty: float,
    fill_ts_ms: int,
    fees: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None
) -> None:
    """Record fill with AI outcome tracking"""
    integration = get_broker_ai_integration(broker_name)
    integration.record_fill_with_ai(
        client_order_id, fill_id, fill_px, fill_qty, fill_ts_ms, fees, extra
    )
