# engine/execution/execution_ai_model.py
"""
Execution Microstructure AI Decision Model

Learns optimal execution behavior:
- Order slicing recommendations
- Delay vs cross decisions  
- When NOT to trade recommendations
- Adaptive execution parameters
"""

import json
import math
import time
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, asdict
from enum import Enum

import numpy as np
from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
from sklearn.linear_model import LinearRegression
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, mean_squared_error

from engine.storage import connect
from engine.execution.execution_ai_features import ExecutionFeatures, get_training_data


class ExecutionAction(Enum):
    """AI-recommended execution actions"""
    SLICE_ORDER = "slice_order"
    DELAY_EXECUTION = "delay_execution"
    CROSS_SPREAD = "cross_spread"
    AVOID_TRADING = "avoid_trading"
    PROCEED_NORMAL = "proceed_normal"


@dataclass
class ExecutionRecommendation:
    """AI execution recommendation"""
    action: ExecutionAction
    confidence: float  # 0-1
    reasoning: str
    
    # Specific parameters
    slice_size: Optional[float] = None
    delay_ms: Optional[int] = None
    limit_offset_bps: Optional[float] = None
    urgency_multiplier: Optional[float] = None
    
    # Expected outcomes
    expected_slippage_bps: Optional[float] = None
    fill_probability: Optional[float] = None
    cost_efficiency: Optional[float] = None


@dataclass
class ModelMetrics:
    """Model performance metrics"""
    train_mae: float
    val_mae: float
    train_rmse: float
    val_rmse: float
    feature_importance: Dict[str, float]
    sample_count: int
    model_version: str


class ExecutionAIModel:
    """Main execution AI model class"""
    
    def __init__(self, model_type: str = "random_forest"):
        self.model_type = model_type
        self.slippage_model = None
        self.fill_prob_model = None
        self.scaler = StandardScaler()
        self.feature_names = []
        self.is_trained = False
        self.model_version = f"v{int(time.time())}"
        
    def _create_slippage_model(self):
        """Create slippage prediction model"""
        if self.model_type == "random_forest":
            return RandomForestRegressor(
                n_estimators=100,
                max_depth=10,
                min_samples_split=20,
                min_samples_leaf=10,
                random_state=42
            )
        elif self.model_type == "gradient_boost":
            return GradientBoostingRegressor(
                n_estimators=100,
                max_depth=6,
                learning_rate=0.1,
                random_state=42
            )
        else:
            return LinearRegression()
    
    def _create_fill_prob_model(self):
        """Create fill probability model"""
        if self.model_type == "random_forest":
            return RandomForestRegressor(
                n_estimators=50,
                max_depth=8,
                min_samples_split=10,
                random_state=42
            )
        else:
            return LinearRegression()
    
    def _prepare_features(self, features_list: List[Dict[str, Any]]) -> np.ndarray:
        """Convert feature dictionaries to numpy array"""
        if not features_list:
            return np.array([])
        
        # Get all feature names consistently
        all_features = set()
        for f in features_list:
            all_features.update(f.keys())
        
        self.feature_names = sorted(list(all_features))
        
        # Create feature matrix
        X = []
        for features in features_list:
            row = []
            for feature_name in self.feature_names:
                value = features.get(feature_name, 0.0)
                # Handle categorical features
                if feature_name in ['aggressiveness', 'market_session', 'venue', 'instrument_type']:
                    # Simple one-hot encoding for common values
                    if feature_name == 'aggressiveness':
                        row.extend([
                            1.0 if value == 'LOW' else 0.0,
                            1.0 if value == 'MEDIUM' else 0.0,
                            1.0 if value == 'HIGH' else 0.0
                        ])
                    elif feature_name == 'market_session':
                        row.extend([
                            1.0 if value == 'pre' else 0.0,
                            1.0 if value == 'market' else 0.0,
                            1.0 if value == 'post' else 0.0
                        ])
                    else:
                        row.append(float(value) if isinstance(value, (int, float)) else 0.0)
                else:
                    row.append(float(value) if isinstance(value, (int, float)) else 0.0)
            X.append(row)
        
        return np.array(X)
    
    def train(self, lookback_days: int = 30, min_samples: int = 100) -> ModelMetrics:
        """Train the execution AI models"""
        
        # Get training data
        X_dict, y_slippage = get_training_data(lookback_days, min_samples)
        
        if len(X_dict) < min_samples:
            raise ValueError(f"Insufficient training data: {len(X_dict)} < {min_samples}")
        
        # Prepare features
        X = self._prepare_features(X_dict)
        
        # Scale features
        X_scaled = self.scaler.fit_transform(X)
        
        # Split data
        X_train, X_val, y_train, y_val = train_test_split(
            X_scaled, y_slippage, test_size=0.2, random_state=42
        )
        
        # Train slippage model
        self.slippage_model = self._create_slippage_model()
        self.slippage_model.fit(X_train, y_train)
        
        # Train fill probability model (using binary fill success as target)
        # For now, use slippage as proxy (lower slippage = higher fill prob)
        y_fill_prob = [1.0 / (1.0 + abs(s)) for s in y_slippage]
        X_train_fp, X_val_fp, y_train_fp, y_val_fp = train_test_split(
            X_scaled, y_fill_prob, test_size=0.2, random_state=42
        )
        
        self.fill_prob_model = self._create_fill_prob_model()
        self.fill_prob_model.fit(X_train_fp, y_train_fp)
        
        # Calculate metrics
        train_pred = self.slippage_model.predict(X_train)
        val_pred = self.slippage_model.predict(X_val)
        
        train_mae = mean_absolute_error(y_train, train_pred)
        val_mae = mean_absolute_error(y_val, val_pred)
        train_rmse = math.sqrt(mean_squared_error(y_train, train_pred))
        val_rmse = math.sqrt(mean_squared_error(y_val, val_pred))
        
        # Feature importance
        feature_importance = {}
        if hasattr(self.slippage_model, 'feature_importances_'):
            for i, importance in enumerate(self.slippage_model.feature_importances_):
                feature_importance[self.feature_names[i]] = float(importance)
        
        self.is_trained = True
        
        # Store model metadata
        self._store_model_metadata(train_mae, val_mae, train_rmse, val_rmse, feature_importance)
        
        return ModelMetrics(
            train_mae=train_mae,
            val_mae=val_mae,
            train_rmse=train_rmse,
            val_rmse=val_rmse,
            feature_importance=feature_importance,
            sample_count=len(X_dict),
            model_version=self.model_version
        )
    
    def _store_model_metadata(self, train_mae: float, val_mae: float, train_rmse: float, val_rmse: float, feature_importance: Dict[str, float]) -> None:
        """Store model training metadata"""
        con = connect()
        try:
            con.execute("""
                INSERT INTO execution_model_training(
                    model_version, training_ts_ms, feature_columns, target_column,
                    sample_count, train_score, val_score, model_params_json,
                    feature_importance_json, created_ts_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                self.model_version,
                int(time.time() * 1000),
                json.dumps(self.feature_names),
                'slippage_bps',
                len(self.feature_names),
                train_mae,
                val_mae,
                json.dumps({
                    'model_type': self.model_type,
                    'train_rmse': train_rmse,
                    'val_rmse': val_rmse
                }),
                json.dumps(feature_importance),
                int(time.time() * 1000)
            ))
            con.commit()
        finally:
            con.close()
    
    def predict_slippage(self, features: ExecutionFeatures) -> float:
        """Predict expected slippage for given features"""
        if not self.is_trained:
            return 2.5  # default slippage
        
        # Convert features to dict
        feature_dict = asdict(features)
        
        # Prepare features
        X = self._prepare_features([feature_dict])
        if X.size == 0:
            return 2.5
        
        # Scale and predict
        X_scaled = self.scaler.transform(X)
        predicted_slippage = self.slippage_model.predict(X_scaled)[0]
        
        return max(0.0, float(predicted_slippage))
    
    def predict_fill_probability(self, features: ExecutionFeatures) -> float:
        """Predict fill probability for given features"""
        if not self.is_trained:
            return 0.95  # default fill probability
        
        # Convert features to dict
        feature_dict = asdict(features)
        
        # Prepare features
        X = self._prepare_features([feature_dict])
        if X.size == 0:
            return 0.95
        
        # Scale and predict
        X_scaled = self.scaler.transform(X)
        predicted_prob = self.fill_prob_model.predict(X_scaled)[0]
        
        return max(0.0, min(1.0, float(predicted_prob)))
    
    def get_execution_recommendation(
        self,
        features: ExecutionFeatures,
        current_spread_bps: float,
        market_impact_estimate: float = 0.0
    ) -> ExecutionRecommendation:
        """Get AI execution recommendation"""
        
        # Predict outcomes
        expected_slippage = self.predict_slippage(features)
        fill_prob = self.predict_fill_probability(features)
        
        # Calculate cost efficiency
        total_expected_cost = expected_slippage + current_spread_bps + market_impact_estimate
        cost_efficiency = 100.0 / max(1.0, total_expected_cost)
        
        # Decision logic based on features and predictions
        
        # 1. Check if we should avoid trading
        if features.urgency_score < 0.2 and expected_slippage > 10.0:
            return ExecutionRecommendation(
                action=ExecutionAction.AVOID_TRADING,
                confidence=0.8,
                reasoning=f"Low urgency ({features.urgency_score:.2f}) and high expected slippage ({expected_slippage:.1f} bps)",
                expected_slippage_bps=expected_slippage,
                fill_probability=fill_prob,
                cost_efficiency=cost_efficiency
            )
        
        # 2. Check for large orders that should be sliced
        if features.size_participation > 0.05 or features.order_size > 10000:
            slice_size = min(features.order_size * 0.2, 5000)  # 20% or max 5000 shares
            
            return ExecutionRecommendation(
                action=ExecutionAction.SLICE_ORDER,
                confidence=0.7,
                reasoning=f"Large order ({features.order_size:.0f} shares, {features.size_participation:.1%} participation)",
                slice_size=slice_size,
                expected_slippage_bps=expected_slippage * 0.7,  # slicing reduces slippage
                fill_probability=fill_prob,
                cost_efficiency=cost_efficiency * 1.2
            )
        
        # 3. Check if we should delay execution
        if (features.volatility > 0.3 and features.urgency_score < 0.5) or \
           (features.depth_imbalance < -0.3 and features.side == -1):  # selling into buying pressure
            
            delay_ms = min(30000, int(features.volatility * 10000))  # up to 30 seconds
            
            return ExecutionRecommendation(
                action=ExecutionAction.DELAY_EXECUTION,
                confidence=0.6,
                reasoning=f"High volatility ({features.volatility:.2%}) or adverse flow ({features.depth_imbalance:.2f})",
                delay_ms=delay_ms,
                expected_slippage_bps=expected_slippage * 0.8,
                fill_probability=fill_prob,
                cost_efficiency=cost_efficiency * 1.1
            )
        
        # 4. Check if we should cross the spread
        if features.urgency_score > 0.8 and current_spread_bps < 5.0 and fill_prob < 0.7:
            return ExecutionRecommendation(
                action=ExecutionAction.CROSS_SPREAD,
                confidence=0.6,
                reasoning=f"High urgency ({features.urgency_score:.2f}) with tight spread ({current_spread_bps:.1f} bps)",
                limit_offset_bps=current_spread_bps * 0.5,
                expected_slippage_bps=current_spread_bps * 0.5,
                fill_probability=min(0.95, fill_prob + 0.2),
                cost_efficiency=cost_efficiency * 0.9
            )
        
        # 5. Default: proceed with normal execution
        limit_offset = max(1.0, expected_slippage * 1.2)
        
        return ExecutionRecommendation(
            action=ExecutionAction.PROCEED_NORMAL,
            confidence=0.5,
            reasoning="Market conditions favorable for normal execution",
            limit_offset_bps=limit_offset,
            expected_slippage_bps=expected_slippage,
            fill_probability=fill_prob,
            cost_efficiency=cost_efficiency
        )


# Global model instance
_execution_ai_model = None


def get_execution_ai_model() -> ExecutionAIModel:
    """Get or create the global execution AI model"""
    global _execution_ai_model
    if _execution_ai_model is None:
        _execution_ai_model = ExecutionAIModel()
    return _execution_ai_model


def train_execution_ai_model(lookback_days: int = 30, min_samples: int = 100) -> ModelMetrics:
    """Train the execution AI model"""
    model = get_execution_ai_model()
    return model.train(lookback_days, min_samples)


def get_execution_recommendation(
    features: ExecutionFeatures,
    current_spread_bps: float,
    market_impact_estimate: float = 0.0
) -> ExecutionRecommendation:
    """Get execution recommendation from AI model"""
    model = get_execution_ai_model()
    return model.get_execution_recommendation(features, current_spread_bps, market_impact_estimate)


def is_model_trained() -> bool:
    """Check if the execution AI model is trained"""
    model = get_execution_ai_model()
    return model.is_trained


def get_model_metrics() -> Optional[ModelMetrics]:
    """Get latest model training metrics"""
    con = connect()
    try:
        row = con.execute("""
            SELECT model_version, training_ts_ms, feature_columns, target_column,
                   sample_count, train_score, val_score, model_params_json,
                   feature_importance_json
            FROM execution_model_training
            ORDER BY created_ts_ms DESC
            LIMIT 1
        """).fetchone()
        
        if not row:
            return None
        
        feature_importance = json.loads(row[8] or '{}')
        
        return ModelMetrics(
            train_mae=float(row[5]),
            val_mae=float(row[6]),
            train_rmse=0.0,  # stored in params_json
            val_rmse=0.0,    # stored in params_json
            feature_importance=feature_importance,
            sample_count=int(row[4]),
            model_version=str(row[0])
        )
        
    finally:
        con.close()
