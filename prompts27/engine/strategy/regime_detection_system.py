"""
Regime Detection and Conditional Routing System

A comprehensive regime detection system that:
1. Defines market regimes (volatility, liquidity, macro stress, trend/range)
2. Detects regimes in real-time
3. Conditions model activation on regime compatibility
4. Prevents model demotion during incompatible regimes
5. Integrates with promotion and capital allocation logic

Asset-class agnostic and production-safe.
"""

import os
import time
import math
import json
import logging
from typing import Dict, Any, List, Optional, Tuple, Set
from dataclasses import dataclass, asdict
from enum import Enum
from datetime import datetime, timedelta

from engine.storage import connect
from engine.strategy.regime_stack import compute_regime_vector, regime_compatibility
from engine.strategy.regime_compat import regime_compat_multiplier, update_regime_compat

logger = logging.getLogger(__name__)


class RegimeType(Enum):
    """Core regime types for classification"""
    VOLATILITY = "volatility"
    LIQUIDITY = "liquidity"
    MACRO_STRESS = "macro_stress"
    TREND_RANGE = "trend_range"


class RegimeState(Enum):
    """Regime state classifications"""
    LOW = "low"
    NORMAL = "normal"
    ELEVATED = "elevated"
    EXTREME = "extreme"


@dataclass
class RegimeDefinition:
    """Definition of a market regime with thresholds and characteristics"""
    name: str
    type: RegimeType
    thresholds: Dict[str, float]
    description: str
    compatible_model_types: List[str]
    incompatible_model_types: List[str]
    risk_multiplier: float
    capital_allocation_factor: float
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RegimeDetectionResult:
    """Result of regime detection for a specific time"""
    timestamp_ms: int
    primary_regime: str
    regime_state: RegimeState
    confidence: float
    regime_vector: Dict[str, Any]
    risk_metrics: Dict[str, float]
    model_compatibility_scores: Dict[str, float]
    recommended_actions: List[str]


@dataclass
class ModelRegimeProfile:
    """Profile defining model's regime preferences and constraints"""
    model_name: str
    model_type: str
    preferred_regimes: List[str]
    avoided_regimes: List[str]
    min_regime_confidence: float
    regime_adaptation_score: float
    last_updated_ms: int
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RegimeDetectionSystem:
    """
    Main regime detection and conditional routing system
    """
    
    def __init__(self):
        self.regime_definitions = self._initialize_regime_definitions()
        self.detection_cache = {}
        self.model_profiles = {}
        self._load_model_profiles()
        
    def _initialize_regime_definitions(self) -> Dict[str, RegimeDefinition]:
        """Initialize comprehensive regime definitions"""
        definitions = {}
        
        # Volatility Regimes
        definitions["vol_low"] = RegimeDefinition(
            name="vol_low",
            type=RegimeType.VOLATILITY,
            thresholds={
                "vix_z": -1.0,
                "rv20_z": -0.8,
                "volatility_ratio": 0.7
            },
            description="Low volatility environment with stable price action",
            compatible_model_types=["mean_reversion", "momentum_slow", "stat_arb"],
            incompatible_model_types=["volatility_breakout", "gamma_scalp"],
            risk_multiplier=0.8,
            capital_allocation_factor=1.2
        )
        
        definitions["vol_normal"] = RegimeDefinition(
            name="vol_normal",
            type=RegimeType.VOLATILITY,
            thresholds={
                "vix_z": 0.0,
                "rv20_z": 0.0,
                "volatility_ratio": 1.0
            },
            description="Normal volatility environment",
            compatible_model_types=["momentum", "mean_reversion", "stat_arb", "trend_following"],
            incompatible_model_types=[],
            risk_multiplier=1.0,
            capital_allocation_factor=1.0
        )
        
        definitions["vol_elevated"] = RegimeDefinition(
            name="vol_elevated",
            type=RegimeType.VOLATILITY,
            thresholds={
                "vix_z": 1.0,
                "rv20_z": 0.8,
                "volatility_ratio": 1.3
            },
            description="Elevated volatility with increased uncertainty",
            compatible_model_types=["volatility_breakout", "momentum_fast", "adaptive"],
            incompatible_model_types=["mean_reversion", "stat_arb"],
            risk_multiplier=1.3,
            capital_allocation_factor=0.8
        )
        
        definitions["vol_extreme"] = RegimeDefinition(
            name="vol_extreme",
            type=RegimeType.VOLATILITY,
            thresholds={
                "vix_z": 2.0,
                "rv20_z": 1.5,
                "volatility_ratio": 1.8
            },
            description="Extreme volatility environment",
            compatible_model_types=["volatility_breakout", "risk_parity", "defensive"],
            incompatible_model_types=["momentum", "mean_reversion", "stat_arb"],
            risk_multiplier=1.8,
            capital_allocation_factor=0.5
        )
        
        # Liquidity Regimes
        definitions["liq_high"] = RegimeDefinition(
            name="liq_high",
            type=RegimeType.LIQUIDITY,
            thresholds={
                "bid_ask_spread_z": -1.0,
                "volume_z": 1.0,
                "market_depth_z": 1.0
            },
            description="High liquidity environment with tight spreads",
            compatible_model_types=["high_frequency", "market_making", "stat_arb"],
            incompatible_model_types=[],
            risk_multiplier=0.9,
            capital_allocation_factor=1.1
        )
        
        definitions["liq_normal"] = RegimeDefinition(
            name="liq_normal",
            type=RegimeType.LIQUIDITY,
            thresholds={
                "bid_ask_spread_z": 0.0,
                "volume_z": 0.0,
                "market_depth_z": 0.0
            },
            description="Normal liquidity conditions",
            compatible_model_types=["momentum", "mean_reversion", "trend_following"],
            incompatible_model_types=[],
            risk_multiplier=1.0,
            capital_allocation_factor=1.0
        )
        
        definitions["liq_low"] = RegimeDefinition(
            name="liq_low",
            type=RegimeType.LIQUIDITY,
            thresholds={
                "bid_ask_spread_z": 1.0,
                "volume_z": -1.0,
                "market_depth_z": -1.0
            },
            description="Low liquidity environment with wide spreads",
            compatible_model_types=["position_trading", "swing_trading", "adaptive"],
            incompatible_model_types=["high_frequency", "market_making", "stat_arb"],
            risk_multiplier=1.4,
            capital_allocation_factor=0.7
        )
        
        # Macro Stress Regimes
        definitions["macro_risk_on"] = RegimeDefinition(
            name="macro_risk_on",
            type=RegimeType.MACRO_STRESS,
            thresholds={
                "credit_spread_z": -0.5,
                "dollar_strength_z": -0.3,
                "risk_appetite_z": 0.8
            },
            description="Risk-on environment with strong risk appetite",
            compatible_model_types=["momentum", "growth", "discretionary"],
            incompatible_model_types=["defensive", "utilities", "value"],
            risk_multiplier=0.9,
            capital_allocation_factor=1.2
        )
        
        definitions["macro_neutral"] = RegimeDefinition(
            name="macro_neutral",
            type=RegimeType.MACRO_STRESS,
            thresholds={
                "credit_spread_z": 0.0,
                "dollar_strength_z": 0.0,
                "risk_appetite_z": 0.0
            },
            description="Neutral macro environment",
            compatible_model_types=["balanced", "adaptive", "multi_strategy"],
            incompatible_model_types=[],
            risk_multiplier=1.0,
            capital_allocation_factor=1.0
        )
        
        definitions["macro_risk_off"] = RegimeDefinition(
            name="macro_risk_off",
            type=RegimeType.MACRO_STRESS,
            thresholds={
                "credit_spread_z": 1.0,
                "dollar_strength_z": 0.5,
                "risk_appetite_z": -1.0
            },
            description="Risk-off environment with flight to safety",
            compatible_model_types=["defensive", "utilities", "value", "quality"],
            incompatible_model_types=["momentum", "growth", "discretionary"],
            risk_multiplier=1.2,
            capital_allocation_factor=0.8
        )
        
        definitions["macro_stress"] = RegimeDefinition(
            name="macro_stress",
            type=RegimeType.MACRO_STRESS,
            thresholds={
                "credit_spread_z": 2.0,
                "dollar_strength_z": 1.0,
                "risk_appetite_z": -2.0
            },
            description="High macro stress with systemic concerns",
            compatible_model_types=["defensive", "cash", "treasury", "gold"],
            incompatible_model_types=["momentum", "growth", "high_beta"],
            risk_multiplier=1.6,
            capital_allocation_factor=0.5
        )
        
        # Trend/Range Regimes
        definitions["trend_strong"] = RegimeDefinition(
            name="trend_strong",
            type=RegimeType.TREND_RANGE,
            thresholds={
                "trend_strength_z": 1.5,
                "price_efficiency_z": 1.0,
                "momentum_persistence": 0.8
            },
            description="Strong trending environment",
            compatible_model_types=["trend_following", "momentum", "breakout"],
            incompatible_model_types=["mean_reversion", "range_trading"],
            risk_multiplier=1.1,
            capital_allocation_factor=1.1
        )
        
        definitions["trend_moderate"] = RegimeDefinition(
            name="trend_moderate",
            type=RegimeType.TREND_RANGE,
            thresholds={
                "trend_strength_z": 0.5,
                "price_efficiency_z": 0.3,
                "momentum_persistence": 0.6
            },
            description="Moderate trending environment",
            compatible_model_types=["trend_following", "momentum", "adaptive"],
            incompatible_model_types=[],
            risk_multiplier=1.0,
            capital_allocation_factor=1.0
        )
        
        definitions["range_bound"] = RegimeDefinition(
            name="range_bound",
            type=RegimeType.TREND_RANGE,
            thresholds={
                "trend_strength_z": -0.5,
                "price_efficiency_z": -0.3,
                "mean_reversion_persistence": 0.7
            },
            description="Range-bound environment",
            compatible_model_types=["mean_reversion", "range_trading", "stat_arb"],
            incompatible_model_types=["trend_following", "momentum"],
            risk_multiplier=0.95,
            capital_allocation_factor=1.05
        )
        
        definitions["choppy"] = RegimeDefinition(
            name="choppy",
            type=RegimeType.TREND_RANGE,
            thresholds={
                "trend_strength_z": -1.0,
                "price_efficiency_z": -1.0,
                "noise_ratio": 0.8
            },
            description="Choppy, directionless environment",
            compatible_model_types=["adaptive", "multi_strategy", "defensive"],
            incompatible_model_types=["trend_following", "momentum", "mean_reversion"],
            risk_multiplier=1.2,
            capital_allocation_factor=0.6
        )
        
        return definitions
    
    def _load_model_profiles(self) -> None:
        """Load model regime profiles from database"""
        con = connect()
        try:
            self._ensure_model_profile_tables(con)
            
            rows = con.execute("""
                SELECT model_name, model_type, preferred_regimes, avoided_regimes,
                       min_regime_confidence, regime_adaptation_score, last_updated_ms
                FROM model_regime_profiles
            """).fetchall()
            
            for row in rows:
                profile = ModelRegimeProfile(
                    model_name=row[0],
                    model_type=row[1],
                    json.loads(row[2]) if row[2] else [],
                    json.loads(row[3]) if row[3] else [],
                    row[4] or 0.5,
                    row[5] or 0.5,
                    row[6] or 0
                )
                self.model_profiles[row[0]] = profile
                
        finally:
            con.close()
    
    def _ensure_model_profile_tables(self, con) -> None:
        """Ensure database tables exist"""
        con.execute("""
            CREATE TABLE IF NOT EXISTS model_regime_profiles (
                model_name TEXT PRIMARY KEY,
                model_type TEXT NOT NULL,
                preferred_regimes TEXT,
                avoided_regimes TEXT,
                min_regime_confidence REAL DEFAULT 0.5,
                regime_adaptation_score REAL DEFAULT 0.5,
                last_updated_ms INTEGER,
                created_at_ms INTEGER DEFAULT (strftime('%s', 'now') * 1000)
            )
        """)
        
        con.execute("""
            CREATE TABLE IF NOT EXISTS regime_detection_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_ms INTEGER NOT NULL,
                primary_regime TEXT NOT NULL,
                regime_state TEXT NOT NULL,
                confidence REAL NOT NULL,
                regime_vector TEXT NOT NULL,
                risk_metrics TEXT NOT NULL,
                model_compatibility_scores TEXT,
                recommended_actions TEXT
            )
        """)
        
        con.execute("""
            CREATE TABLE IF NOT EXISTS regime_model_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_ms INTEGER NOT NULL,
                model_name TEXT NOT NULL,
                action_type TEXT NOT NULL,
                regime_name TEXT NOT NULL,
                reason TEXT NOT NULL,
                compatibility_score REAL,
                risk_multiplier REAL,
                capital_adjustment REAL
            )
        """)
        
        con.commit()
    
    def detect_regime(self, symbol: Optional[str] = None, ts_ms: Optional[int] = None) -> RegimeDetectionResult:
        """
        Detect current market regime for a symbol and timestamp
        """
        timestamp = int(ts_ms or time.time() * 1000)
        
        # Get regime vector from existing system
        regime_vector = compute_regime_vector(symbol=symbol, ts_ms=timestamp)
        
        # Calculate risk metrics
        risk_metrics = self._calculate_risk_metrics(regime_vector)
        
        # Determine primary regime and state
        primary_regime, confidence = self._determine_primary_regime(regime_vector, risk_metrics)
        regime_state = self._determine_regime_state(risk_metrics)
        
        # Calculate model compatibility scores
        model_compatibility_scores = self._calculate_model_compatibility_scores(regime_vector)
        
        # Generate recommended actions
        recommended_actions = self._generate_recommended_actions(
            primary_regime, regime_state, confidence, model_compatibility_scores
        )
        
        result = RegimeDetectionResult(
            timestamp_ms=timestamp,
            primary_regime=primary_regime,
            regime_state=regime_state,
            confidence=confidence,
            regime_vector=regime_vector,
            risk_metrics=risk_metrics,
            model_compatibility_scores=model_compatibility_scores,
            recommended_actions=recommended_actions
        )
        
        # Cache result
        cache_key = f"{symbol}_{timestamp}"
        self.detection_cache[cache_key] = result
        
        # Store in database
        self._store_detection_result(result)
        
        return result
    
    def _calculate_risk_metrics(self, regime_vector: Dict[str, Any]) -> Dict[str, float]:
        """Calculate comprehensive risk metrics from regime vector"""
        risk_metrics = {}
        
        macro = regime_vector.get("macro", {})
        asset = regime_vector.get("asset", {})
        micro = regime_vector.get("micro", {})
        
        # Volatility risk
        risk_metrics["volatility_risk"] = float(macro.get("vol_expansion", 0.0))
        
        # Credit risk
        risk_metrics["credit_risk"] = float(macro.get("credit_stress", 0.0))
        
        # Market risk
        risk_metrics["market_risk"] = float(macro.get("risk_off", 0.0))
        
        # Liquidity risk (inverse of auction activity)
        risk_metrics["liquidity_risk"] = 1.0 - float(micro.get("auction_heavy", 0.5))
        
        # News shock risk
        risk_metrics["news_shock_risk"] = float(micro.get("news_shock", 0.0))
        
        # Social churn risk
        risk_metrics["social_churn_risk"] = float(micro.get("social_churn", 0.0))
        
        # Composite risk score
        risk_metrics["composite_risk"] = (
            risk_metrics["volatility_risk"] * 0.25 +
            risk_metrics["credit_risk"] * 0.20 +
            risk_metrics["market_risk"] * 0.20 +
            risk_metrics["liquidity_risk"] * 0.15 +
            risk_metrics["news_shock_risk"] * 0.10 +
            risk_metrics["social_churn_risk"] * 0.10
        )
        
        return risk_metrics
    
    def _determine_primary_regime(self, regime_vector: Dict[str, Any], risk_metrics: Dict[str, float]) -> Tuple[str, float]:
        """Determine the primary regime based on vector and risk metrics"""
        macro = regime_vector.get("macro", {})
        micro = regime_vector.get("micro", {})
        
        # Volatility-based regime
        vol_expansion = float(macro.get("vol_expansion", 0.0))
        if vol_expansion > 0.8:
            return "vol_extreme", 0.9
        elif vol_expansion > 0.5:
            return "vol_elevated", 0.8
        elif vol_expansion < 0.2:
            return "vol_low", 0.7
        else:
            return "vol_normal", 0.6
        
        # TODO: Add logic for other regime types based on risk metrics
        # This is simplified for initial implementation
    
    def _determine_regime_state(self, risk_metrics: Dict[str, float]) -> RegimeState:
        """Determine regime state based on risk metrics"""
        composite_risk = risk_metrics.get("composite_risk", 0.0)
        
        if composite_risk > 0.8:
            return RegimeState.EXTREME
        elif composite_risk > 0.6:
            return RegimeState.ELEVATED
        elif composite_risk > 0.3:
            return RegimeState.NORMAL
        else:
            return RegimeState.LOW
    
    def _calculate_model_compatibility_scores(self, regime_vector: Dict[str, Any]) -> Dict[str, float]:
        """Calculate compatibility scores for all models"""
        scores = {}
        
        for model_name, profile in self.model_profiles.items():
            # Create regime profile for this model
            model_profile = {
                "preferred_regimes": profile.preferred_regimes,
                "avoided_regimes": profile.avoided_regimes,
                "min_confidence": profile.min_regime_confidence
            }
            
            # Calculate compatibility using existing system
            compatibility = regime_compatibility(model_profile, regime_vector)
            scores[model_name] = compatibility
        
        return scores
    
    def _generate_recommended_actions(self, primary_regime: str, regime_state: RegimeState, 
                                   confidence: float, model_scores: Dict[str, float]) -> List[str]:
        """Generate recommended actions based on regime detection"""
        actions = []
        
        # Regime-based actions
        if regime_state == RegimeState.EXTREME:
            actions.extend([
                "REDUCE_EXPOSURE",
                "ACTIVATE_DEFENSIVE_MODELS",
                "INCREASE_RISK_MULTIPLIER"
            ])
        elif regime_state == RegimeState.ELEVATED:
            actions.extend([
                "MONITOR_CLOSELY",
                "CONSIDER_POSITION_REDUCTION"
            ])
        
        # Model-specific actions
        low_performers = [m for m, s in model_scores.items() if s < 0.3]
        high_performers = [m for m, s in model_scores.items() if s > 0.8]
        
        if low_performers:
            actions.append(f"CONSIDER_DEMOTION: {', '.join(low_performers[:3])}")
        
        if high_performers:
            actions.append(f"PRIORITY_PROMOTION: {', '.join(high_performers[:3])}")
        
        # Confidence-based actions
        if confidence < 0.5:
            actions.append("LOW_CONFIDENCE - INCREASE_CAUTION")
        
        return actions
    
    def _store_detection_result(self, result: RegimeDetectionResult) -> None:
        """Store detection result in database"""
        con = connect()
        try:
            self._ensure_model_profile_tables(con)
            
            con.execute("""
                INSERT INTO regime_detection_history 
                (timestamp_ms, primary_regime, regime_state, confidence, 
                 regime_vector, risk_metrics, model_compatibility_scores, recommended_actions)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                result.timestamp_ms,
                result.primary_regime,
                result.regime_state.value,
                result.confidence,
                json.dumps(result.regime_vector),
                json.dumps(result.risk_metrics),
                json.dumps(result.model_compatibility_scores),
                json.dumps(result.recommended_actions)
            ))
            
            con.commit()
        except Exception as e:
            logger.error(f"Failed to store detection result: {e}")
        finally:
            con.close()
    
    def should_promote_model(self, model_name: str, current_regime: str, 
                           promotion_metrics: Dict[str, float]) -> Tuple[bool, str]:
        """
        Determine if a model should be promoted based on regime compatibility
        """
        # Get model profile
        profile = self.model_profiles.get(model_name)
        if not profile:
            return False, "No model profile found"
        
        # Check if current regime is preferred
        if current_regime in profile.avoided_regimes:
            return False, f"Regime {current_regime} is avoided by model"
        
        # Check minimum confidence requirement
        if profile.min_regime_confidence > 0.5:
            # Get current regime detection
            detection = self.detect_regime()
            if detection.confidence < profile.min_regime_confidence:
                return False, f"Regime confidence {detection.confidence:.2f} below threshold {profile.min_regime_confidence}"
        
        # Check regime compatibility score
        detection = self.detect_regime()
        compatibility = detection.model_compatibility_scores.get(model_name, 0.0)
        
        if compatibility < 0.4:
            return False, f"Low regime compatibility: {compatibility:.2f}"
        
        # Check if regime is in preferred list
        if profile.preferred_regimes and current_regime not in profile.preferred_regimes:
            # Still allow promotion if compatibility is high enough
            if compatibility < 0.7:
                return False, f"Regime not preferred and compatibility moderate: {compatibility:.2f}"
        
        return True, "Regime compatibility check passed"
    
    def should_demote_model(self, model_name: str, current_regime: str,
                          demotion_metrics: Dict[str, float]) -> Tuple[bool, str]:
        """
        Determine if a model should be demoted, with regime protection
        """
        # Get model profile
        profile = self.model_profiles.get(model_name)
        if not profile:
            return False, "No model profile found"
        
        # Check if current regime is preferred (protection from demotion)
        if current_regime in profile.preferred_regimes:
            # Get current regime detection
            detection = self.detect_regime()
            compatibility = detection.model_compatibility_scores.get(model_name, 0.0)
            
            # High compatibility in preferred regime provides protection
            if compatibility > 0.7:
                return False, f"Model in preferred regime {current_regime} with high compatibility {compatibility:.2f}"
        
        # Check if current regime is avoided (more likely to demote)
        if current_regime in profile.avoided_regimes:
            detection = self.detect_regime()
            compatibility = detection.model_compatibility_scores.get(model_name, 0.0)
            
            # Low compatibility in avoided regime increases demotion likelihood
            if compatibility < 0.3:
                return True, f"Model in avoided regime {current_regime} with low compatibility {compatibility:.2f}"
        
        # Standard demotion logic applies if no regime protection
        return False, "No regime-based demotion trigger"
    
    def get_capital_allocation_adjustment(self, model_name: str, current_regime: str) -> float:
        """
        Get capital allocation adjustment factor based on regime compatibility
        """
        # Get regime definition
        regime_def = self.regime_definitions.get(current_regime)
        if not regime_def:
            return 1.0
        
        # Get model profile
        profile = self.model_profiles.get(model_name)
        if not profile:
            return regime_def.capital_allocation_factor
        
        # Check if model is compatible with this regime
        if profile.model_type in regime_def.compatible_model_types:
            return regime_def.capital_allocation_factor * 1.1  # Bonus for compatibility
        elif profile.model_type in regime_def.incompatible_model_types:
            return regime_def.capital_allocation_factor * 0.7  # Penalty for incompatibility
        
        return regime_def.capital_allocation_factor
    
    def update_model_performance_in_regime(self, model_name: str, regime: str, 
                                         performance_metrics: Dict[str, float]) -> None:
        """
        Update model performance tracking for regime compatibility learning
        """
        # Calculate net return for regime compatibility update
        net_return = performance_metrics.get("net_return", 0.0)
        
        # Update existing regime compatibility system
        update_regime_compat(
            model_name=model_name,
            regime=regime,
            net_return=net_return
        )
        
        # Log the update
        logger.info(f"Updated regime compatibility for {model_name} in {regime}: {net_return:.4f}")
    
    def get_regime_summary(self, hours_back: int = 24) -> Dict[str, Any]:
        """
        Get summary of regime activity over specified time period
        """
        con = connect()
        try:
            cutoff_ms = int((time.time() - hours_back * 3600) * 1000)
            
            rows = con.execute("""
                SELECT primary_regime, regime_state, COUNT(*) as count, AVG(confidence) as avg_confidence
                FROM regime_detection_history
                WHERE timestamp_ms >= ?
                GROUP BY primary_regime, regime_state
                ORDER BY count DESC
            """, (cutoff_ms,)).fetchall()
            
            summary = {
                "period_hours": hours_back,
                "regime_distribution": {},
                "total_detections": 0,
                "avg_confidence": 0.0
            }
            
            total_count = 0
            weighted_confidence = 0.0
            
            for row in rows:
                regime, state, count, avg_conf = row
                key = f"{regime}_{state}"
                summary["regime_distribution"][key] = {
                    "count": count,
                    "avg_confidence": avg_conf
                }
                total_count += count
                weighted_confidence += count * avg_conf
            
            summary["total_detections"] = total_count
            summary["avg_confidence"] = weighted_confidence / total_count if total_count > 0 else 0.0
            
            return summary
            
        finally:
            con.close()


# Global instance
_regime_detection_system = None

def get_regime_detection_system() -> RegimeDetectionSystem:
    """Get singleton instance of regime detection system"""
    global _regime_detection_system
    if _regime_detection_system is None:
        _regime_detection_system = RegimeDetectionSystem()
    return _regime_detection_system


# Convenience functions for integration
def detect_current_regime(symbol: Optional[str] = None) -> RegimeDetectionResult:
    """Detect current regime for a symbol"""
    system = get_regime_detection_system()
    return system.detect_regime(symbol=symbol)


def should_promote_model_regime_aware(model_name: str, promotion_metrics: Dict[str, float]) -> Tuple[bool, str]:
    """Check if model should be promoted with regime awareness"""
    system = get_regime_detection_system()
    current_detection = system.detect_regime()
    return system.should_promote_model(model_name, current_detection.primary_regime, promotion_metrics)


def should_demote_model_regime_aware(model_name: str, demotion_metrics: Dict[str, float]) -> Tuple[bool, str]:
    """Check if model should be demoted with regime protection"""
    system = get_regime_detection_system()
    current_detection = system.detect_regime()
    return system.should_demote_model(model_name, current_detection.primary_regime, demotion_metrics)


def get_regime_aware_capital_adjustment(model_name: str) -> float:
    """Get capital allocation adjustment based on regime compatibility"""
    system = get_regime_detection_system()
    current_detection = system.detect_regime()
    return system.get_capital_allocation_adjustment(model_name, current_detection.primary_regime)
