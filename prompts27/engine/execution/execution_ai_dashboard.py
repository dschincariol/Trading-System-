# engine/execution/execution_ai_dashboard.py
"""
Execution AI Dashboard Analytics & Diagnostics

Provides comprehensive analytics and diagnostics for the Execution Microstructure AI system.
Integrates with existing dashboard infrastructure.
"""

import json
import time
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, asdict

from engine.storage import connect
from engine.execution.execution_ai_advisor import get_execution_ai_advisor
from engine.execution.execution_ai_model import get_model_metrics, is_model_trained
from engine.execution.execution_analytics_engine import summarize_execution_performance


@dataclass
class AIDashboardSummary:
    """AI dashboard summary data"""
    model_status: str
    model_version: str
    last_training_ts_ms: int
    model_performance: Dict[str, float]
    
    advisory_stats: Dict[str, Any]
    recent_recommendations: List[Dict[str, Any]]
    
    execution_performance: Dict[str, Any]
    cost_efficiency_trend: List[Dict[str, float]]
    
    feature_importance: Dict[str, float]
    top_features: List[Tuple[str, float]]


class ExecutionAIDashboard:
    """AI dashboard analytics provider"""
    
    def __init__(self):
        self.advisor = get_execution_ai_advisor()
    
    def get_dashboard_summary(self, days: int = 7) -> AIDashboardSummary:
        """Get comprehensive dashboard summary"""
        
        # Model status
        model_status = "not_trained" if not is_model_trained() else "active"
        model_version = "v1.0"
        last_training_ts_ms = 0
        model_performance = {}
        
        if is_model_trained():
            metrics = get_model_metrics()
            if metrics:
                model_version = metrics.model_version
                model_performance = {
                    'train_mae': metrics.train_mae,
                    'val_mae': metrics.val_mae,
                    'sample_count': metrics.sample_count
                }
        
        # Advisory statistics
        advisory_stats = self.advisor.get_advisory_stats(days)
        
        # Recent recommendations
        recent_recommendations = self._get_recent_recommendations(limit=10)
        
        # Execution performance
        execution_performance = summarize_execution_performance(days)
        
        # Cost efficiency trend
        cost_efficiency_trend = self._get_cost_efficiency_trend(days)
        
        # Feature importance
        feature_importance = {}
        top_features = []
        if is_model_trained():
            metrics = get_model_metrics()
            if metrics and metrics.feature_importance:
                feature_importance = metrics.feature_importance
                top_features = sorted(
                    feature_importance.items(), 
                    key=lambda x: x[1], 
                    reverse=True
                )[:10]
        
        return AIDashboardSummary(
            model_status=model_status,
            model_version=model_version,
            last_training_ts_ms=last_training_ts_ms,
            model_performance=model_performance,
            advisory_stats=advisory_stats,
            recent_recommendations=recent_recommendations,
            execution_performance=execution_performance,
            cost_efficiency_trend=cost_efficiency_trend,
            feature_importance=feature_importance,
            top_features=top_features
        )
    
    def _get_recent_recommendations(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent AI recommendations"""
        con = connect()
        try:
            rows = con.execute("""
                SELECT recommendation_id, client_order_id, broker, symbol, action, confidence, reasoning,
                       created_ts_ms, status, approved_by, approval_ts_ms,
                       expected_slippage_bps, fill_probability, cost_efficiency
                FROM execution_ai_advisory
                ORDER BY created_ts_ms DESC
                LIMIT ?
            """, (limit,)).fetchall()
            
            recommendations = []
            for row in rows:
                recommendations.append({
                    'recommendation_id': row[0],
                    'client_order_id': row[1],
                    'broker': row[2],
                    'symbol': row[3],
                    'action': row[4],
                    'confidence': float(row[5]),
                    'reasoning': row[6],
                    'created_ts_ms': int(row[7]),
                    'status': row[8],
                    'approved_by': row[9],
                    'approval_ts_ms': int(row[10]) if row[10] else None,
                    'expected_slippage_bps': float(row[11]) if row[11] else None,
                    'fill_probability': float(row[12]) if row[12] else None,
                    'cost_efficiency': float(row[13]) if row[13] else None
                })
            
            return recommendations
            
        finally:
            con.close()
    
    def _get_cost_efficiency_trend(self, days: int = 7) -> List[Dict[str, float]]:
        """Get cost efficiency trend over time"""
        con = connect()
        try:
            since_ts = int(time.time() * 1000) - (days * 24 * 3600 * 1000)
            
            # Group by day
            rows = con.execute("""
                SELECT 
                    DATE(ts_ms / 1000, 'unixepoch') as date,
                    AVG(cost_efficiency) as avg_efficiency,
                    AVG(expected_slippage_bps) as avg_slippage,
                    COUNT(*) as recommendation_count
                FROM execution_ai_advisory
                WHERE created_ts_ms > ? AND cost_efficiency IS NOT NULL
                GROUP BY DATE(ts_ms / 1000, 'unixepoch')
                ORDER BY date DESC
            """, (since_ts,)).fetchall()
            
            trend = []
            for row in rows:
                trend.append({
                    'date': row[0],
                    'avg_efficiency': float(row[1]) if row[1] else 0,
                    'avg_slippage_bps': float(row[2]) if row[2] else 0,
                    'recommendation_count': int(row[3])
                })
            
            return trend
            
        finally:
            con.close()
    
    def get_action_performance_breakdown(self, days: int = 7) -> Dict[str, Any]:
        """Get performance breakdown by AI action type"""
        con = connect()
        try:
            since_ts = int(time.time() * 1000) - (days * 24 * 3600 * 1000)
            
            # Get executed recommendations with outcomes
            rows = con.execute("""
                SELECT 
                    a.action,
                    COUNT(*) as total_recommendations,
                    SUM(CASE WHEN a.status = 'approved' THEN 1 ELSE 0 END) as approved,
                    SUM(CASE WHEN a.status = 'executed' THEN 1 ELSE 0 END) as executed,
                    AVG(a.confidence) as avg_confidence,
                    AVG(CAST(JSON_EXTRACT(a.execution_outcome_json, '$.actual_slippage_bps') AS REAL)) as avg_actual_slip,
                    AVG(a.expected_slippage_bps) as avg_expected_slip,
                    AVG(CAST(JSON_EXTRACT(a.execution_outcome_json, '$.actual_cost_bps') AS REAL)) as avg_actual_cost
                FROM execution_ai_advisory a
                WHERE a.created_ts_ms > ?
                GROUP BY a.action
                ORDER BY total_recommendations DESC
            """, (since_ts,)).fetchall()
            
            breakdown = {}
            for row in rows:
                action = row[0]
                breakdown[action] = {
                    'total_recommendations': int(row[1]),
                    'approved': int(row[2]),
                    'executed': int(row[3]),
                    'approval_rate': float(row[2]) / max(1, int(row[1])),
                    'execution_rate': float(row[3]) / max(1, int(row[2])),
                    'avg_confidence': float(row[4]) if row[4] else 0,
                    'avg_actual_slippage_bps': float(row[5]) if row[5] else 0,
                    'avg_expected_slippage_bps': float(row[6]) if row[6] else 0,
                    'slippage_prediction_error': abs(float(row[5] or 0) - float(row[6] or 0)),
                    'avg_actual_cost_bps': float(row[7]) if row[7] else 0
                }
            
            return breakdown
            
        finally:
            con.close()
    
    def get_feature_analysis(self, days: int = 7) -> Dict[str, Any]:
        """Get detailed feature analysis"""
        con = connect()
        try:
            since_ts = int(time.time() * 1000) - (days * 24 * 3600 * 1000)
            
            # Feature correlations with slippage
            feature_correlations = {}
            
            # Get recent features with outcomes
            rows = con.execute("""
                SELECT features_json, actual_slippage_bps
                FROM execution_features
                WHERE ts_ms > ? AND actual_slippage_bps IS NOT NULL
                LIMIT 1000
            """, (since_ts,)).fetchall()
            
            if len(rows) >= 50:
                # Calculate correlations for key features
                feature_data = {}
                
                for features_json, actual_slippage in rows:
                    try:
                        features = json.loads(features_json or '{}')
                        if isinstance(features, dict):
                            for feature_name, feature_value in features.items():
                                if isinstance(feature_value, (int, float)):
                                    if feature_name not in feature_data:
                                        feature_data[feature_name] = []
                                    feature_data[feature_name].append((feature_value, float(actual_slippage)))
                    except Exception:
                        continue
                
                # Calculate correlations
                for feature_name, values in feature_data.items():
                    if len(values) >= 10:
                        correlation = self._calculate_correlation(values)
                        if correlation is not None:
                            feature_correlations[feature_name] = correlation
            
            # Feature distributions
            feature_distributions = {}
            for feature_name, corr in feature_correlations.items():
                if abs(corr) > 0.1:  # Only include features with meaningful correlation
                    feature_distributions[feature_name] = self._get_feature_distribution(feature_name, days)
            
            return {
                'feature_correlations': feature_correlations,
                'feature_distributions': feature_distributions,
                'top_predictive_features': sorted(
                    feature_correlations.items(), 
                    key=lambda x: abs(x[1]), 
                    reverse=True
                )[:10]
            }
            
        finally:
            con.close()
    
    def _calculate_correlation(self, pairs: List[Tuple[float, float]]) -> Optional[float]:
        """Calculate Pearson correlation coefficient"""
        if len(pairs) < 2:
            return None
        
        x_values = [p[0] for p in pairs]
        y_values = [p[1] for p in pairs]
        
        n = len(pairs)
        sum_x = sum(x_values)
        sum_y = sum(y_values)
        sum_xy = sum(x * y for x, y in pairs)
        sum_x2 = sum(x * x for x in x_values)
        sum_y2 = sum(y * y for y in y_values)
        
        numerator = n * sum_xy - sum_x * sum_y
        denominator = ((n * sum_x2 - sum_x * sum_x) * (n * sum_y2 - sum_y * sum_y)) ** 0.5
        
        if denominator == 0:
            return None
        
        return numerator / denominator
    
    def _get_feature_distribution(self, feature_name: str, days: int) -> Dict[str, float]:
        """Get feature distribution statistics"""
        con = connect()
        try:
            since_ts = int(time.time() * 1000) - (days * 24 * 3600 * 1000)
            
            # Extract feature values from JSON
            rows = con.execute("""
                SELECT features_json
                FROM execution_features
                WHERE ts_ms > ?
                LIMIT 1000
            """, (since_ts,)).fetchall()
            
            values = []
            for row in rows:
                try:
                    features = json.loads(row[0] or '{}')
                    if isinstance(features, dict) and feature_name in features:
                        value = features[feature_name]
                        if isinstance(value, (int, float)):
                            values.append(float(value))
                except Exception:
                    continue
            
            if not values:
                return {}
            
            values.sort()
            n = len(values)
            
            return {
                'min': values[0],
                'max': values[-1],
                'mean': sum(values) / n,
                'median': values[n // 2],
                'p25': values[n // 4],
                'p75': values[3 * n // 4],
                'std': (sum((x - sum(values) / n) ** 2 for x in values) / n) ** 0.5,
                'count': n
            }
            
        finally:
            con.close()
    
    def get_model_diagnostics(self) -> Dict[str, Any]:
        """Get detailed model diagnostics"""
        if not is_model_trained():
            return {
                'status': 'not_trained',
                'message': 'AI model has not been trained yet'
            }
        
        metrics = get_model_metrics()
        if not metrics:
            return {
                'status': 'no_metrics',
                'message': 'No model metrics available'
            }
        
        # Get training data quality metrics
        training_quality = self._get_training_data_quality()
        
        # Get prediction accuracy over time
        prediction_accuracy = self._get_prediction_accuracy_trend()
        
        return {
            'status': 'active',
            'model_version': metrics.model_version,
            'performance_metrics': {
                'train_mae': metrics.train_mae,
                'val_mae': metrics.val_mae,
                'sample_count': metrics.sample_count,
                'overfitting_indicator': metrics.train_mae / max(0.1, metrics.val_mae)
            },
            'feature_importance': metrics.feature_importance,
            'training_data_quality': training_quality,
            'prediction_accuracy_trend': prediction_accuracy,
            'last_updated': int(time.time() * 1000)
        }
    
    def _get_training_data_quality(self) -> Dict[str, Any]:
        """Get training data quality metrics"""
        con = connect()
        try:
            # Get feature data quality
            rows = con.execute("""
                SELECT 
                    COUNT(*) as total_samples,
                    COUNT(CASE WHEN actual_slippage_bps IS NOT NULL THEN 1 END) as labeled_samples,
                    AVG(CASE WHEN actual_slippage_bps IS NOT NULL THEN ABS(actual_slippage_bps) END) as avg_slippage,
                    COUNT(DISTINCT broker) as broker_count,
                    COUNT(DISTINCT symbol) as symbol_count
                FROM execution_features
                WHERE ts_ms > ?
            """, (int(time.time() * 1000) - 30 * 24 * 3600 * 1000,)).fetchone()  # 30 days
            
            if not rows or rows[0] == 0:
                return {'status': 'no_data'}
            
            total_samples, labeled_samples, avg_slippage, broker_count, symbol_count = rows
            
            return {
                'total_samples': int(total_samples),
                'labeled_samples': int(labeled_samples),
                'labeling_rate': float(labeled_samples) / max(1, total_samples),
                'avg_slippage_bps': float(avg_slippage) if avg_slippage else 0,
                'broker_diversity': int(broker_count),
                'symbol_diversity': int(symbol_count),
                'data_freshness_days': 30
            }
            
        finally:
            con.close()
    
    def _get_prediction_accuracy_trend(self, days: int = 7) -> List[Dict[str, float]]:
        """Get prediction accuracy trend over time"""
        con = connect()
        try:
            since_ts = int(time.time() * 1000) - (days * 24 * 3600 * 1000)
            
            rows = con.execute("""
                SELECT 
                    DATE(f.fill_ts_ms / 1000, 'unixepoch') as date,
                    AVG(ABS(f.fill_px - o.ref_px) / o.ref_px * 10000) as avg_actual_slippage,
                    AVG(a.expected_slippage_bps) as avg_predicted_slippage,
                    COUNT(*) as sample_count
                FROM execution_fills f
                JOIN execution_orders o ON f.client_order_id = o.client_order_id
                JOIN execution_ai_advisory a ON o.client_order_id = a.client_order_id
                WHERE f.fill_ts_ms > ? 
                  AND o.ref_px IS NOT NULL 
                  AND o.ref_px > 0
                  AND a.expected_slippage_bps IS NOT NULL
                GROUP BY DATE(f.fill_ts_ms / 1000, 'unixepoch')
                ORDER BY date DESC
            """, (since_ts,)).fetchall()
            
            trend = []
            for row in rows:
                actual_slip = float(row[1]) if row[1] else 0
                predicted_slip = float(row[2]) if row[2] else 0
                
                trend.append({
                    'date': row[0],
                    'avg_actual_slippage_bps': actual_slip,
                    'avg_predicted_slippage_bps': predicted_slip,
                    'prediction_error_bps': abs(actual_slip - predicted_slip),
                    'sample_count': int(row[3])
                })
            
            return trend
            
        finally:
            con.close()


# Global dashboard instance
_execution_ai_dashboard = None


def get_execution_ai_dashboard() -> ExecutionAIDashboard:
    """Get or create the global execution AI dashboard"""
    global _execution_ai_dashboard
    if _execution_ai_dashboard is None:
        _execution_ai_dashboard = ExecutionAIDashboard()
    return _execution_ai_dashboard


def get_ai_dashboard_data(days: int = 7) -> Dict[str, Any]:
    """Get AI dashboard data for frontend"""
    dashboard = get_execution_ai_dashboard()
    summary = dashboard.get_dashboard_summary(days)
    
    return {
        'model_status': summary.model_status,
        'model_version': summary.model_version,
        'model_performance': summary.model_performance,
        'advisory_stats': summary.advisory_stats,
        'recent_recommendations': summary.recent_recommendations,
        'execution_performance': summary.execution_performance,
        'cost_efficiency_trend': summary.cost_efficiency_trend,
        'feature_importance': summary.feature_importance,
        'top_features': summary.top_features
    }


def get_ai_action_performance(days: int = 7) -> Dict[str, Any]:
    """Get AI action performance breakdown"""
    dashboard = get_execution_ai_dashboard()
    return dashboard.get_action_performance_breakdown(days)


def get_ai_feature_analysis(days: int = 7) -> Dict[str, Any]:
    """Get AI feature analysis"""
    dashboard = get_execution_ai_dashboard()
    return dashboard.get_feature_analysis(days)


def get_ai_model_diagnostics() -> Dict[str, Any]:
    """Get AI model diagnostics"""
    dashboard = get_execution_ai_dashboard()
    return dashboard.get_model_diagnostics()
