"""
Tests for Regime Detection System

Comprehensive test suite covering:
- Regime definitions and detection
- Model compatibility scoring
- Promotion/demotion decisions
- Capital allocation adjustments
- Integration with existing systems
"""

import pytest
import time
import json
from unittest.mock import Mock, patch, MagicMock
from typing import Dict, Any

from engine.strategy.regime_detection_system import (
    RegimeDetectionSystem, RegimeDefinition, RegimeType, RegimeState,
    ModelRegimeProfile, get_regime_detection_system,
    detect_current_regime, should_promote_model_regime_aware,
    should_demote_model_regime_aware, get_regime_aware_capital_adjustment
)
from engine.strategy.regime_stack import compute_regime_vector


class TestRegimeDefinitions:
    """Test regime definitions and initialization"""
    
    def test_regime_definitions_initialization(self):
        """Test that all expected regimes are properly defined"""
        system = RegimeDetectionSystem()
        
        # Check that we have all expected regime types
        regime_types = set(def.type for def in system.regime_definitions.values())
        expected_types = {RegimeType.VOLATILITY, RegimeType.LIQUIDITY, 
                         RegimeType.MACRO_STRESS, RegimeType.TREND_RANGE}
        
        assert regime_types == expected_types
        
        # Check volatility regimes
        vol_regimes = [r for r in system.regime_definitions.values() 
                      if r.type == RegimeType.VOLATILITY]
        assert len(vol_regimes) == 4  # low, normal, elevated, extreme
        
        # Check that each regime has required fields
        for regime_def in system.regime_definitions.values():
            assert regime_def.name
            assert regime_def.type
            assert isinstance(regime_def.thresholds, dict)
            assert regime_def.description
            assert isinstance(regime_def.compatible_model_types, list)
            assert isinstance(regime_def.incompatible_model_types, list)
            assert regime_def.risk_multiplier > 0
            assert regime_def.capital_allocation_factor > 0
    
    def test_regime_thresholds(self):
        """Test that regime thresholds are logically ordered"""
        system = RegimeDetectionSystem()
        
        # Check volatility regimes ordering
        vol_low = system.regime_definitions["vol_low"]
        vol_normal = system.regime_definitions["vol_normal"]
        vol_elevated = system.regime_definitions["vol_elevated"]
        vol_extreme = system.regime_definitions["vol_extreme"]
        
        # VIX Z-scores should increase with volatility level
        assert vol_low.thresholds["vix_z"] < vol_normal.thresholds["vix_z"]
        assert vol_normal.thresholds["vix_z"] < vol_elevated.thresholds["vix_z"]
        assert vol_elevated.thresholds["vix_z"] < vol_extreme.thresholds["vix_z"]
        
        # Risk multipliers should increase with volatility
        assert vol_low.risk_multiplier < vol_normal.risk_multiplier
        assert vol_normal.risk_multiplier < vol_elevated.risk_multiplier
        assert vol_elevated.risk_multiplier < vol_extreme.risk_multiplier
        
        # Capital allocation factors should decrease with volatility
        assert vol_low.capital_allocation_factor > vol_normal.capital_allocation_factor
        assert vol_normal.capital_allocation_factor > vol_elevated.capital_allocation_factor
        assert vol_elevated.capital_allocation_factor > vol_extreme.capital_allocation_factor


class TestRegimeDetection:
    """Test regime detection logic"""
    
    @patch('engine.strategy.regime_detection_system.compute_regime_vector')
    def test_detect_regime_basic(self, mock_compute_vector):
        """Test basic regime detection"""
        # Mock regime vector
        mock_vector = {
            "macro": {
                "risk_off": 0.2,
                "risk_on": 0.8,
                "vol_expansion": 0.1,
                "credit_stress": 0.1
            },
            "asset": {
                "etf_like": 1.0,
                "single_stock_like": 0.0
            },
            "micro": {
                "momentum_dominant": 0.3,
                "auction_heavy": 0.6,
                "news_shock": 0.2,
                "social_churn": 0.1
            }
        }
        mock_compute_vector.return_value = mock_vector
        
        system = RegimeDetectionSystem()
        result = system.detect_regime(symbol="SPY")
        
        # Check result structure
        assert result.timestamp_ms > 0
        assert result.primary_regime
        assert result.regime_state
        assert 0 <= result.confidence <= 1
        assert result.regime_vector == mock_vector
        assert isinstance(result.risk_metrics, dict)
        assert isinstance(result.model_compatibility_scores, dict)
        assert isinstance(result.recommended_actions, list)
        
        # Low volatility should be detected
        assert result.primary_regime == "vol_low"
        assert result.regime_state == RegimeState.NORMAL
    
    @patch('engine.strategy.regime_detection_system.compute_regime_vector')
    def test_detect_regime_extreme_volatility(self, mock_compute_vector):
        """Test regime detection with extreme volatility"""
        # Mock high volatility regime vector
        mock_vector = {
            "macro": {
                "risk_off": 0.9,
                "risk_on": 0.1,
                "vol_expansion": 0.9,
                "credit_stress": 0.7
            },
            "asset": {
                "etf_like": 1.0,
                "single_stock_like": 0.0
            },
            "micro": {
                "momentum_dominant": 0.8,
                "auction_heavy": 0.1,
                "news_shock": 0.8,
                "social_churn": 0.6
            }
        }
        mock_compute_vector.return_value = mock_vector
        
        system = RegimeDetectionSystem()
        result = system.detect_regime(symbol="SPY")
        
        # Extreme volatility should be detected
        assert result.primary_regime == "vol_extreme"
        assert result.regime_state == RegimeState.EXTREME
        assert result.confidence > 0.8
        
        # Risk metrics should reflect high risk
        assert result.risk_metrics["volatility_risk"] > 0.8
        assert result.risk_metrics["composite_risk"] > 0.7
    
    def test_calculate_risk_metrics(self):
        """Test risk metrics calculation"""
        system = RegimeDetectionSystem()
        
        regime_vector = {
            "macro": {
                "vol_expansion": 0.6,
                "credit_stress": 0.3,
                "risk_off": 0.4
            },
            "micro": {
                "auction_heavy": 0.7,
                "news_shock": 0.4,
                "social_churn": 0.2
            }
        }
        
        risk_metrics = system._calculate_risk_metrics(regime_vector)
        
        # Check all risk metrics are present
        expected_metrics = [
            "volatility_risk", "credit_risk", "market_risk",
            "liquidity_risk", "news_shock_risk", "social_churn_risk", "composite_risk"
        ]
        
        for metric in expected_metrics:
            assert metric in risk_metrics
            assert 0 <= risk_metrics[metric] <= 1
        
        # Check composite risk calculation
        expected_composite = (
            0.6 * 0.25 +  # volatility_risk
            0.3 * 0.20 +  # credit_risk
            0.4 * 0.20 +  # market_risk
            0.3 * 0.15 +  # liquidity_risk (1 - auction_heavy)
            0.4 * 0.10 +  # news_shock_risk
            0.2 * 0.10    # social_churn_risk
        )
        
        assert abs(risk_metrics["composite_risk"] - expected_composite) < 0.01


class TestModelCompatibility:
    """Test model compatibility scoring and decisions"""
    
    def setup_method(self):
        """Set up test model profiles"""
        self.system = RegimeDetectionSystem()
        
        # Add test model profiles
        self.momentum_profile = ModelRegimeProfile(
            model_name="momentum_model",
            model_type="momentum",
            preferred_regimes=["trend_strong", "vol_normal"],
            avoided_regimes=["range_bound", "choppy"],
            min_regime_confidence=0.6,
            regime_adaptation_score=0.7,
            last_updated_ms=int(time.time() * 1000)
        )
        
        self.mean_reversion_profile = ModelRegimeProfile(
            model_name="mean_reversion_model",
            model_type="mean_reversion",
            preferred_regimes=["range_bound", "vol_low"],
            avoided_regimes=["trend_strong", "vol_extreme"],
            min_regime_confidence=0.5,
            regime_adaptation_score=0.8,
            last_updated_ms=int(time.time() * 1000)
        )
        
        self.system.model_profiles["momentum_model"] = self.momentum_profile
        self.system.model_profiles["mean_reversion_model"] = self.mean_reversion_profile
    
    @patch('engine.strategy.regime_detection_system.compute_regime_vector')
    def test_model_compatibility_scoring(self, mock_compute_vector):
        """Test model compatibility scoring"""
        # Mock trending regime
        mock_vector = {
            "macro": {"vol_expansion": 0.3, "credit_stress": 0.1, "risk_off": 0.2},
            "asset": {"etf_like": 1.0, "single_stock_like": 0.0},
            "micro": {"momentum_dominant": 0.8, "auction_heavy": 0.5, 
                     "news_shock": 0.3, "social_churn": 0.2}
        }
        mock_compute_vector.return_value = mock_vector
        
        result = self.system.detect_regime()
        compatibility_scores = result.model_compatibility_scores
        
        # Momentum model should have higher compatibility in trending regime
        assert compatibility_scores["momentum_model"] > compatibility_scores["mean_reversion_model"]
    
    def test_should_promote_model_compatible(self):
        """Test promotion decision for compatible model"""
        promotion_metrics = {
            "sharpe_ratio": 0.8,
            "win_rate": 0.6,
            "profit_factor": 1.5
        }
        
        can_promote, reason = self.system.should_promote_model(
            "momentum_model", "trend_strong", promotion_metrics
        )
        
        assert can_promote
        assert "compatible" in reason.lower()
    
    def test_should_promote_model_incompatible(self):
        """Test promotion decision for incompatible model"""
        promotion_metrics = {
            "sharpe_ratio": 0.8,
            "win_rate": 0.6,
            "profit_factor": 1.5
        }
        
        can_promote, reason = self.system.should_promote_model(
            "momentum_model", "range_bound", promotion_metrics
        )
        
        # Should be blocked due to avoided regime
        assert not can_promote
        assert "avoided" in reason.lower()
    
    def test_should_demote_model_protection(self):
        """Test demotion protection in preferred regime"""
        demotion_metrics = {
            "sharpe_ratio": 0.3,  # Low but not critical
            "win_rate": 0.45,
            "drawdown": 0.18
        }
        
        should_demote, reason = self.system.should_demote_model(
            "momentum_model", "trend_strong", demotion_metrics
        )
        
        # Should be protected in preferred regime
        assert not should_demote
        assert "preferred" in reason.lower()
    
    def test_should_demote_model_no_protection(self):
        """Test demotion without protection"""
        demotion_metrics = {
            "sharpe_ratio": 0.1,  # Very low
            "win_rate": 0.35,
            "drawdown": 0.25
        }
        
        should_demote, reason = self.system.should_demote_model(
            "momentum_model", "range_bound", demotion_metrics
        )
        
        # Should be demoted in avoided regime with poor performance
        assert should_demote
        assert "avoided" in reason.lower()
    
    def test_capital_allocation_adjustment(self):
        """Test capital allocation adjustment based on regime"""
        # Compatible model in compatible regime
        adjustment = self.system.get_capital_allocation_adjustment(
            "momentum_model", "trend_strong"
        )
        assert adjustment > 1.0  # Should get bonus
        
        # Incompatible model in incompatible regime
        adjustment = self.system.get_capital_allocation_adjustment(
            "momentum_model", "range_bound"
        )
        assert adjustment < 1.0  # Should get penalty


class TestIntegration:
    """Test integration with existing systems"""
    
    def test_singleton_instances(self):
        """Test that singleton instances work correctly"""
        system1 = get_regime_detection_system()
        system2 = get_regime_detection_system()
        
        assert system1 is system2
    
    @patch('engine.strategy.regime_detection_system.get_regime_detection_system')
    def test_convenience_functions(self, mock_get_system):
        """Test convenience functions"""
        mock_system = Mock()
        mock_get_system.return_value = mock_system
        
        # Mock detection result
        mock_result = Mock()
        mock_result.primary_regime = "vol_normal"
        mock_result.confidence = 0.7
        mock_result.model_compatibility_scores = {"test_model": 0.8}
        mock_system.detect_regime.return_value = mock_result
        
        # Test promotion function
        can_promote, reason = should_promote_model_regime_aware("test_model", {})
        assert isinstance(can_promote, bool)
        assert isinstance(reason, str)
        
        # Test demotion function
        should_demote, reason = should_demote_model_regime_aware("test_model", {})
        assert isinstance(should_demote, bool)
        assert isinstance(reason, str)
        
        # Test capital adjustment function
        adjustment = get_regime_aware_capital_adjustment("test_model")
        assert isinstance(adjustment, float)


class TestErrorHandling:
    """Test error handling and edge cases"""
    
    def test_missing_model_profile(self):
        """Test behavior when model profile is missing"""
        system = RegimeDetectionSystem()
        
        can_promote, reason = system.should_promote_model(
            "nonexistent_model", "vol_normal", {}
        )
        
        assert not can_promote
        assert "no model profile" in reason.lower()
    
    def test_invalid_regime_definition(self):
        """Test behavior with invalid regime definition"""
        system = RegimeDetectionSystem()
        
        adjustment = system.get_capital_allocation_adjustment(
            "test_model", "nonexistent_regime"
        )
        
        assert adjustment == 1.0  # Should default to neutral
    
    @patch('engine.strategy.regime_detection_system.compute_regime_vector')
    def test_regime_detection_with_error(self, mock_compute_vector):
        """Test regime detection when compute_regime_vector fails"""
        mock_compute_vector.side_effect = Exception("Test error")
        
        system = RegimeDetectionSystem()
        
        # Should handle error gracefully
        with pytest.raises(Exception):
            system.detect_regime()


class TestPerformance:
    """Test performance and caching"""
    
    def test_detection_caching(self):
        """Test that detection results are cached"""
        system = RegimeDetectionSystem()
        
        with patch.object(system, 'detect_regime') as mock_detect:
            mock_result = Mock()
            mock_result.timestamp_ms = int(time.time() * 1000)
            mock_detect.return_value = mock_result
            
            # First call should invoke the method
            result1 = system.detect_regime(symbol="SPY", ts_ms=1234567890)
            
            # Second call with same parameters should use cache
            result2 = system.detect_regime(symbol="SPY", ts_ms=1234567890)
            
            # Should only call once due to caching
            mock_detect.assert_called_once()
            assert result1 is result2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
