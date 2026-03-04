"""
Regime Detection Job

Periodic job that:
1. Runs regime detection across all monitored assets
2. Updates model compatibility scores
3. Triggers regime-aware governance decisions
4. Adjusts capital allocation based on regime
5. Maintains regime history and analytics

Production-safe with error handling and monitoring.
"""

import os
import time
import json
import logging
import schedule
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

from engine.storage import connect
from engine.strategy.regime_detection_system import get_regime_detection_system
from engine.strategy.regime_aware_governance import get_regime_aware_governance
from engine.strategy.model_governance import evaluate_all_models_for_promotion, evaluate_all_models_for_demotion
from engine.strategy.capital_allocation_engine import run_capital_allocation
from engine.strategy.decision_log import log_decision

# Configure logging
logger = logging.getLogger(__name__)

# Configuration
DETECTION_INTERVAL_MINUTES = int(os.environ.get("REGIME_DETECTION_INTERVAL_MIN", "5"))
ENABLE_GOVERNANCE_INTEGRATION = os.environ.get("REGIME_GOVERNANCE_INTEGRATION", "1") == "1"
ENABLE_CAPITAL_ADJUSTMENT = os.environ.get("REGIME_CAPITAL_ADJUSTMENT", "1") == "1"
MONITORED_SYMBOLS = os.environ.get("REGIME_MONITORED_SYMBOLS", "SPY,QQQ,IWM").split(",")


class RegimeDetectionJob:
    """
    Main regime detection job that runs periodically
    """
    
    def __init__(self):
        self.regime_system = get_regime_detection_system()
        self.governance_system = get_regime_aware_governance()
        self.last_run_time = 0
        self.error_count = 0
        self.max_errors = 10
        
    def run_detection_cycle(self) -> Dict[str, Any]:
        """
        Run a complete regime detection cycle
        """
        cycle_start = time.time()
        results = {
            "cycle_start_ms": int(cycle_start * 1000),
            "symbol_detections": {},
            "governance_decisions": {},
            "capital_adjustments": {},
            "errors": [],
            "success": False
        }
        
        try:
            logger.info("Starting regime detection cycle")
            
            # 1. Detect regimes for all monitored symbols
            symbol_results = self._detect_regimes_for_symbols()
            results["symbol_detections"] = symbol_results
            
            # 2. Update model compatibility scores
            self._update_model_compatibility_scores(symbol_results)
            
            # 3. Run regime-aware governance decisions
            if ENABLE_GOVERNANCE_INTEGRATION:
                governance_results = self._run_governance_decisions()
                results["governance_decisions"] = governance_results
            
            # 4. Adjust capital allocation
            if ENABLE_CAPITAL_ADJUSTMENT:
                capital_results = self._adjust_capital_allocation()
                results["capital_adjustments"] = capital_results
            
            # 5. Update analytics and monitoring
            self._update_analytics(symbol_results)
            
            results["success"] = True
            self.error_count = 0  # Reset error count on success
            
            cycle_duration = time.time() - cycle_start
            logger.info(f"Regime detection cycle completed in {cycle_duration:.2f}s")
            
        except Exception as e:
            error_msg = f"Regime detection cycle failed: {str(e)}"
            logger.error(error_msg, exc_info=True)
            results["errors"].append(error_msg)
            self.error_count += 1
            
            # Log decision for monitoring
            log_decision(
                decision_type="regime_detection_error",
                details={
                    "error": str(e),
                    "error_count": self.error_count,
                    "cycle_start_ms": results["cycle_start_ms"]
                }
            )
        
        results["cycle_duration_ms"] = int((time.time() - cycle_start) * 1000)
        self.last_run_time = cycle_start
        
        return results
    
    def _detect_regimes_for_symbols(self) -> Dict[str, Any]:
        """
        Detect regimes for all monitored symbols
        """
        symbol_results = {}
        
        for symbol in MONITORED_SYMBOLS:
            symbol = symbol.strip()
            if not symbol:
                continue
                
            try:
                detection = self.regime_system.detect_regime(symbol=symbol)
                symbol_results[symbol] = {
                    "primary_regime": detection.primary_regime,
                    "regime_state": detection.regime_state.value,
                    "confidence": detection.confidence,
                    "risk_metrics": detection.risk_metrics,
                    "recommended_actions": detection.recommended_actions
                }
                
                logger.debug(f"Detected regime for {symbol}: {detection.primary_regime} "
                           f"(confidence: {detection.confidence:.2f})")
                
            except Exception as e:
                error_msg = f"Failed to detect regime for {symbol}: {str(e)}"
                logger.error(error_msg)
                symbol_results[symbol] = {"error": error_msg}
        
        return symbol_results
    
    def _update_model_compatibility_scores(self, symbol_results: Dict[str, Any]) -> None:
        """
        Update model compatibility scores based on current regime
        """
        try:
            # Get average regime across all symbols
            all_regimes = []
            for symbol_data in symbol_results.values():
                if "primary_regime" in symbol_data:
                    all_regimes.append(symbol_data["primary_regime"])
            
            if not all_regimes:
                logger.warning("No valid regime detections found for compatibility updates")
                return
            
            # Use most common regime as the market-wide regime
            market_regime = max(set(all_regimes), key=all_regimes.count)
            
            # Update compatibility scores for all models
            detection = self.regime_system.detect_regime()
            for model_name, compatibility_score in detection.model_compatibility_scores.items():
                logger.debug(f"Model {model_name} compatibility with {market_regime}: {compatibility_score:.3f}")
                
        except Exception as e:
            logger.error(f"Failed to update model compatibility scores: {str(e)}")
    
    def _run_governance_decisions(self) -> Dict[str, Any]:
        """
        Run regime-aware governance decisions for all models
        """
        governance_results = {}
        
        try:
            # Get all models that need evaluation
            promotion_candidates = evaluate_all_models_for_promotion()
            demotion_candidates = evaluate_all_models_for_demotion()
            
            # Evaluate promotion candidates
            for model_info in promotion_candidates:
                model_name = model_info.get("model_name")
                model_type = model_info.get("model_type", "unknown")
                governance_metrics = model_info.get("metrics", {})
                
                try:
                    decision = self.governance_system.evaluate_promotion_with_regime(
                        model_name, model_type, governance_metrics
                    )
                    
                    governance_results[f"promotion_{model_name}"] = {
                        "recommended_action": decision.recommended_action,
                        "regime": decision.regime_name,
                        "compatibility": decision.compatibility_score,
                        "reasoning": decision.reasoning
                    }
                    
                    # Log the decision
                    log_decision(
                        decision_type="regime_aware_promotion",
                        model_name=model_name,
                        details={
                            "action": decision.recommended_action,
                            "regime": decision.regime_name,
                            "compatibility": decision.compatibility_score,
                            "reasoning": decision.reasoning
                        }
                    )
                    
                except Exception as e:
                    logger.error(f"Failed to evaluate promotion for {model_name}: {str(e)}")
                    governance_results[f"promotion_{model_name}"] = {"error": str(e)}
            
            # Evaluate demotion candidates
            for model_info in demotion_candidates:
                model_name = model_info.get("model_name")
                model_type = model_info.get("model_type", "unknown")
                governance_metrics = model_info.get("metrics", {})
                
                try:
                    decision = self.governance_system.evaluate_demotion_with_regime(
                        model_name, model_type, governance_metrics
                    )
                    
                    governance_results[f"demotion_{model_name}"] = {
                        "recommended_action": decision.recommended_action,
                        "regime": decision.regime_name,
                        "compatibility": decision.compatibility_score,
                        "reasoning": decision.reasoning
                    }
                    
                    # Log the decision
                    log_decision(
                        decision_type="regime_aware_demotion",
                        model_name=model_name,
                        details={
                            "action": decision.recommended_action,
                            "regime": decision.regime_name,
                            "compatibility": decision.compatibility_score,
                            "reasoning": decision.reasoning
                        }
                    )
                    
                except Exception as e:
                    logger.error(f"Failed to evaluate demotion for {model_name}: {str(e)}")
                    governance_results[f"demotion_{model_name}"] = {"error": str(e)}
            
        except Exception as e:
            logger.error(f"Failed to run governance decisions: {str(e)}")
            governance_results["error"] = str(e)
        
        return governance_results
    
    def _adjust_capital_allocation(self) -> Dict[str, Any]:
        """
        Adjust capital allocation based on current regime
        """
        capital_results = {}
        
        try:
            # Get current allocation targets
            current_targets = run_capital_allocation()
            
            # Apply regime-aware adjustments
            adjusted_targets = self.governance_system.adjust_capital_allocation_with_regime(current_targets)
            
            # Calculate adjustment summary
            total_original = sum(t.weight for t in current_targets)
            total_adjusted = sum(t.weight for t in adjusted_targets)
            
            capital_results = {
                "original_total_weight": total_original,
                "adjusted_total_weight": total_adjusted,
                "adjustment_factor": total_adjusted / total_original if total_original > 0 else 1.0,
                "target_count": len(adjusted_targets),
                "significant_adjustments": []
            }
            
            # Track significant adjustments (>10% change)
            for orig, adj in zip(current_targets, adjusted_targets):
                if orig.strategy == adj.strategy and orig.asset == adj.asset:
                    change_pct = abs(adj.weight - orig.weight) / orig.weight if orig.weight > 0 else 0
                    if change_pct > 0.1:
                        capital_results["significant_adjustments"].append({
                            "strategy": orig.strategy,
                            "asset": orig.asset,
                            "original_weight": orig.weight,
                            "adjusted_weight": adj.weight,
                            "change_pct": change_pct
                        })
            
            logger.info(f"Capital allocation adjusted: {total_original:.3f} -> {total_adjusted:.3f} "
                       f"({len(capital_results['significant_adjustments'])} significant adjustments)")
            
        except Exception as e:
            logger.error(f"Failed to adjust capital allocation: {str(e)}")
            capital_results["error"] = str(e)
        
        return capital_results
    
    def _update_analytics(self, symbol_results: Dict[str, Any]) -> None:
        """
        Update analytics and monitoring data
        """
        try:
            # Store regime detection summary
            summary = self.regime_system.get_regime_summary(hours_back=24)
            
            log_decision(
                decision_type="regime_detection_summary",
                details={
                    "summary": summary,
                    "symbol_count": len(symbol_results),
                    "cycle_timestamp": int(time.time() * 1000)
                }
            )
            
        except Exception as e:
            logger.error(f"Failed to update analytics: {str(e)}")
    
    def is_healthy(self) -> bool:
        """
        Check if the job is healthy
        """
        if self.error_count >= self.max_errors:
            return False
        
        # Check if last run was recent (within 2x expected interval)
        expected_interval = DETECTION_INTERVAL_MINUTES * 60
        if time.time() - self.last_run_time > expected_interval * 2:
            return False
        
        return True
    
    def get_status(self) -> Dict[str, Any]:
        """
        Get current job status
        """
        return {
            "last_run_time": self.last_run_time,
            "error_count": self.error_count,
            "max_errors": self.max_errors,
            "healthy": self.is_healthy(),
            "detection_interval_minutes": DETECTION_INTERVAL_MINUTES,
            "governance_integration": ENABLE_GOVERNANCE_INTEGRATION,
            "capital_adjustment": ENABLE_CAPITAL_ADJUSTMENT,
            "monitored_symbols": MONITORED_SYMBOLS
        }


# Global job instance
_regime_detection_job = None

def get_regime_detection_job() -> RegimeDetectionJob:
    """Get singleton instance of regime detection job"""
    global _regime_detection_job
    if _regime_detection_job is None:
        _regime_detection_job = RegimeDetectionJob()
    return _regime_detection_job


def run_regime_detection_cycle() -> Dict[str, Any]:
    """
    Run a single regime detection cycle
    """
    job = get_regime_detection_job()
    return job.run_detection_cycle()


def start_regime_detection_scheduler():
    """
    Start the regime detection scheduler
    """
    job = get_regime_detection_job()
    
    # Schedule periodic detection
    schedule.every(DETECTION_INTERVAL_MINUTES).minutes.do(job.run_detection_cycle)
    
    logger.info(f"Regime detection scheduler started (interval: {DETECTION_INTERVAL_MINUTES} minutes)")
    
    # Run immediately on start
    job.run_detection_cycle()
    
    # Keep the scheduler running
    while True:
        try:
            schedule.run_pending()
            time.sleep(60)  # Check every minute
        except KeyboardInterrupt:
            logger.info("Regime detection scheduler stopped by user")
            break
        except Exception as e:
            logger.error(f"Scheduler error: {str(e)}")
            time.sleep(60)


if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [regime_detection_job] %(message)s"
    )
    
    # Start the scheduler
    start_regime_detection_scheduler()
