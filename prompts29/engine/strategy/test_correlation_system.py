"""
Test Suite for Enhanced Correlation and Crowding Control System

Tests for:
- System stability under load
- Low latency performance
- Accuracy of correlation calculations
- Effectiveness of crowding detection
- Integration with capital allocation
"""

import unittest
import time
import numpy as np
from typing import Dict, List, Any
import threading
import concurrent.futures
from dataclasses import dataclass
import json

from engine.strategy.enhanced_correlation_analyzer import (
    EnhancedCorrelationAnalyzer, 
    ModelMetrics, 
    CorrelationMetrics, 
    CrowdingMetrics
)
from engine.strategy.crowding_penalty_system import (
    CrowdingPenaltySystem, 
    PenaltyConfig, 
    PenaltyBreakdown
)
from engine.strategy.concentration_risk_controller import (
    ConcentrationRiskController, 
    ConcentrationLimits, 
    RiskLevel
)
from engine.strategy.capital_allocation_engine import (
    CapitalAllocationEngine, 
    AllocationTarget, 
    CapitalConstraints
)
from engine.strategy.risk_monitoring_system import (
    RiskMonitoringSystem, 
    MonitoringConfig, 
    AlertSeverity, 
    AlertCategory
)


class TestEnhancedCorrelationSystem(unittest.TestCase):
    """Comprehensive test suite for the enhanced correlation system"""
    
    def setUp(self):
        """Set up test environment"""
        self.correlation_analyzer = EnhancedCorrelationAnalyzer(lookback_days=30)
        self.penalty_system = CrowdingPenaltySystem()
        self.concentration_controller = ConcentrationRiskController()
        self.monitoring_system = RiskMonitoringSystem()
        
        # Register systems with monitoring
        self.monitoring_system.register_systems(
            self.correlation_analyzer,
            self.penalty_system,
            self.concentration_controller
        )
        
        # Test data
        self.test_models = self._create_test_models()
        self.test_alerts = self._create_test_alerts()
    
    def _create_test_models(self) -> List[Dict[str, Any]]:
        """Create test model data"""
        return [
            {
                'model_id': 'momentum_aapl_1h',
                'strategy_type': 'momentum',
                'asset': 'AAPL',
                'horizon': '1h',
                'pnl_history': np.random.normal(0.001, 0.02, 50).tolist(),
                'factor_exposures': {'momentum': 0.8, 'size': 0.2, 'value': -0.1}
            },
            {
                'model_id': 'mean_reversion_msft_1h',
                'strategy_type': 'mean_reversion',
                'asset': 'MSFT',
                'horizon': '1h',
                'pnl_history': np.random.normal(0.0008, 0.015, 50).tolist(),
                'factor_exposures': {'momentum': -0.3, 'size': 0.1, 'value': 0.6}
            },
            {
                'model_id': 'momentum_googl_4h',
                'strategy_type': 'momentum',
                'asset': 'GOOGL',
                'horizon': '4h',
                'pnl_history': np.random.normal(0.0012, 0.025, 50).tolist(),
                'factor_exposures': {'momentum': 0.9, 'size': 0.3, 'volatility': 0.2}
            },
            {
                'model_id': 'volatility_jpm_1d',
                'strategy_type': 'volatility',
                'asset': 'JPM',
                'horizon': '1d',
                'pnl_history': np.random.normal(0.0005, 0.01, 50).tolist(),
                'factor_exposures': {'volatility': 0.7, 'momentum': 0.1, 'quality': 0.2}
            },
            {
                'model_id': 'macro_xom_1w',
                'strategy_type': 'macro',
                'asset': 'XOM',
                'horizon': '1w',
                'pnl_history': np.random.normal(0.002, 0.03, 50).tolist(),
                'factor_exposures': {'macro': 0.8, 'energy': 0.6, 'value': 0.3}
            }
        ]
    
    def _create_test_alerts(self) -> List[Dict[str, Any]]:
        """Create test alert data for allocation"""
        return [
            {
                'strategy': 'momentum',
                'symbol': 'AAPL',
                'expected_z': 1.5,
                'confidence': 0.8,
                'horizon': '1h'
            },
            {
                'strategy': 'mean_reversion',
                'symbol': 'MSFT',
                'expected_z': 1.2,
                'confidence': 0.75,
                'horizon': '1h'
            },
            {
                'strategy': 'momentum',
                'symbol': 'GOOGL',
                'expected_z': 1.8,
                'confidence': 0.85,
                'horizon': '4h'
            },
            {
                'strategy': 'volatility',
                'symbol': 'JPM',
                'expected_z': 1.1,
                'confidence': 0.7,
                'horizon': '1d'
            },
            {
                'strategy': 'macro',
                'symbol': 'XOM',
                'expected_z': 1.3,
                'confidence': 0.72,
                'horizon': '1w'
            }
        ]
    
    def test_correlation_analyzer_basic_functionality(self):
        """Test basic correlation analyzer functionality"""
        # Add test models
        for model_data in self.test_models:
            self.correlation_analyzer.update_model_metrics(
                model_id=model_data['model_id'],
                strategy_type=model_data['strategy_type'],
                asset=model_data['asset'],
                horizon=model_data['horizon'],
                pnl=model_data['pnl_history'][-1] if model_data['pnl_history'] else None,
                factor_exposures=model_data['factor_exposures']
            )
        
        # Test correlation calculation
        model1, model2 = self.test_models[0]['model_id'], self.test_models[1]['model_id']
        correlation_metrics = self.correlation_analyzer.calculate_model_correlation(model1, model2)
        
        self.assertIsInstance(correlation_metrics, CorrelationMetrics)
        self.assertGreaterEqual(correlation_metrics.combined_correlation, 0.0)
        self.assertLessEqual(correlation_metrics.combined_correlation, 1.0)
        
        # Test crowding detection
        crowding_metrics = self.correlation_analyzer.detect_crowding(model1)
        self.assertIsInstance(crowding_metrics, CrowdingMetrics)
        self.assertGreaterEqual(crowding_metrics.overall_crowding, 0.0)
        self.assertLessEqual(crowding_metrics.overall_crowding, 1.0)
    
    def test_penalty_system_accuracy(self):
        """Test penalty system accuracy and effectiveness"""
        # Set up test data
        for model_data in self.test_models:
            self.correlation_analyzer.update_model_metrics(
                model_id=model_data['model_id'],
                strategy_type=model_data['strategy_type'],
                asset=model_data['asset'],
                horizon=model_data['horizon'],
                pnl=model_data['pnl_history'][-1] if model_data['pnl_history'] else None,
                factor_exposures=model_data['factor_exposures']
            )
        
        # Test penalty calculation
        test_allocations = []
        for alert in self.test_alerts:
            test_allocations.append({
                'model_id': f"{alert['strategy']}_{alert['symbol']}_{alert['horizon']}",
                'strategy': alert['strategy'],
                'asset': alert['symbol'],
                'horizon': alert['horizon'],
                'weight': 0.1,  # 10% weight
                'expected_return': alert['expected_z'],
                'confidence': alert['confidence']
            })
        
        penalized_allocations = self.penalty_system.apply_penalties_to_allocations(test_allocations)
        
        # Verify penalties are applied
        self.assertEqual(len(penalized_allocations), len(test_allocations))
        
        for alloc in penalized_allocations:
            self.assertIn('penalty_adjusted_weight', alloc)
            self.assertIn('penalty_breakdown', alloc)
            self.assertLessEqual(alloc['penalty_adjusted_weight'], alloc['weight'])
            
            penalty_breakdown = alloc['penalty_breakdown']
            self.assertIsInstance(penalty_breakdown, PenaltyBreakdown)
            self.assertGreaterEqual(penalty_breakdown.total_penalty, 0.0)
            self.assertLessEqual(penalty_breakdown.total_penalty, 1.0)
    
    def test_concentration_risk_prevention(self):
        """Test concentration risk prevention mechanisms"""
        # Create concentrated portfolio
        concentrated_allocations = [
            {'model_id': 'test1', 'strategy': 'momentum', 'asset': 'AAPL', 'horizon': '1h', 'weight': 0.15},
            {'model_id': 'test2', 'strategy': 'trend', 'asset': 'AAPL', 'horizon': '4h', 'weight': 0.12},
            {'model_id': 'test3', 'strategy': 'breakout', 'asset': 'MSFT', 'horizon': '1h', 'weight': 0.25},
            {'model_id': 'test4', 'strategy': 'momentum', 'asset': 'MSFT', 'horizon': '1d', 'weight': 0.18},
            {'model_id': 'test5', 'strategy': 'mean_reversion', 'asset': 'GOOGL', 'horizon': '1h', 'weight': 0.20}
        ]
        
        self.concentration_controller.update_portfolio(concentrated_allocations)
        
        # Check for concentration violations
        risk_summary = self.concentration_controller.get_risk_summary()
        self.assertIn('concentration_metrics', risk_summary)
        self.assertIn('active_alerts', risk_summary)
        
        # Test allocation checking
        new_allocation = {'model_id': 'test6', 'strategy': 'momentum', 'asset': 'AAPL', 'horizon': '1h', 'weight': 0.10}
        allowed, violations = self.concentration_controller.check_allocation_allowed(new_allocation)
        
        # Should have violations due to high AAPL exposure
        if not allowed:
            self.assertGreater(len(violations), 0)
    
    def test_integration_with_capital_allocation(self):
        """Test integration with capital allocation engine"""
        # Create enhanced constraints
        constraints = CapitalConstraints(
            enable_crowding_penalties=True,
            enable_concentration_limits=True,
            max_crowding_score=0.4
        )
        
        engine = CapitalAllocationEngine(constraints)
        
        # Run allocation
        start_time = time.time()
        targets = engine.allocate_capital(self.test_alerts)
        allocation_time = time.time() - start_time
        
        # Verify results
        self.assertIsInstance(targets, list)
        self.assertLess(allocation_time, 1.0)  # Should complete within 1 second
        
        for target in targets:
            self.assertIsInstance(target, AllocationTarget)
            self.assertGreaterEqual(target.weight, 0.0)
            self.assertLessEqual(target.weight, constraints.max_position_size)
            
            # Check penalty fields
            self.assertGreaterEqual(target.crowding_penalty, 0.0)
            self.assertGreaterEqual(target.concentration_penalty, 0.0)
            self.assertIsInstance(target.penalty_breakdown, dict)
    
    def test_monitoring_system_functionality(self):
        """Test monitoring system functionality"""
        # Set up test data
        for model_data in self.test_models:
            self.correlation_analyzer.update_model_metrics(
                model_id=model_data['model_id'],
                strategy_type=model_data['strategy_type'],
                asset=model_data['asset'],
                horizon=model_data['horizon'],
                pnl=model_data['pnl_history'][-1] if model_data['pnl_history'] else None,
                factor_exposures=model_data['factor_exposures']
            )
        
        # Start monitoring
        self.monitoring_system.start_monitoring()
        
        # Wait for monitoring cycle
        time.sleep(2)
        
        # Check monitoring status
        metrics_summary = self.monitoring_system.get_metrics_summary()
        self.assertEqual(metrics_summary['monitoring_status'], 'active')
        self.assertIn('current_metrics', metrics_summary)
        self.assertIn('active_alerts_count', metrics_summary)
        
        # Stop monitoring
        self.monitoring_system.stop_monitoring()
    
    def test_performance_under_load(self):
        """Test system performance under load"""
        # Generate large dataset
        large_model_set = []
        for i in range(100):  # 100 models
            large_model_set.append({
                'model_id': f'test_model_{i}',
                'strategy_type': np.random.choice(['momentum', 'mean_reversion', 'volatility', 'macro']),
                'asset': np.random.choice(['AAPL', 'MSFT', 'GOOGL', 'JPM', 'XOM']),
                'horizon': np.random.choice(['5m', '1h', '4h', '1d']),
                'pnl_history': np.random.normal(0.001, 0.02, 50).tolist(),
                'factor_exposures': {
                    'momentum': np.random.uniform(-1, 1),
                    'value': np.random.uniform(-1, 1),
                    'size': np.random.uniform(-1, 1)
                }
            })
        
        # Test correlation calculation performance
        start_time = time.time()
        
        for model_data in large_model_set:
            self.correlation_analyzer.update_model_metrics(
                model_id=model_data['model_id'],
                strategy_type=model_data['strategy_type'],
                asset=model_data['asset'],
                horizon=model_data['horizon'],
                pnl=model_data['pnl_history'][-1],
                factor_exposures=model_data['factor_exposures']
            )
        
        correlation_time = time.time() - start_time
        
        # Test batch correlation calculations
        start_time = time.time()
        for i in range(0, min(50, len(large_model_set)), 2):  # Test 25 pairs
            model1 = large_model_set[i]['model_id']
            model2 = large_model_set[i+1]['model_id']
            self.correlation_analyzer.calculate_model_correlation(model1, model2)
        
        batch_correlation_time = time.time() - start_time
        
        # Performance assertions
        self.assertLess(correlation_time, 5.0)  # Should complete within 5 seconds
        self.assertLess(batch_correlation_time, 2.0)  # Batch should be faster
        
        print(f"Performance: {len(large_model_set)} models in {correlation_time:.3f}s")
        print(f"Batch correlation: 25 pairs in {batch_correlation_time:.3f}s")
    
    def test_concurrent_access(self):
        """Test system stability under concurrent access"""
        def worker_function(worker_id):
            """Worker function for concurrent testing"""
            results = []
            for i in range(10):
                model_id = f'worker_{worker_id}_model_{i}'
                self.correlation_analyzer.update_model_metrics(
                    model_id=model_id,
                    strategy_type='momentum',
                    asset='AAPL',
                    horizon='1h',
                    pnl=np.random.normal(0, 0.02),
                    factor_exposures={'momentum': np.random.uniform(-1, 1)}
                )
                
                # Test crowding detection
                crowding = self.correlation_analyzer.detect_crowding(model_id)
                results.append(crowding.overall_crowding)
            
            return results
        
        # Run concurrent workers
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(worker_function, i) for i in range(5)]
            results = [future.result() for future in concurrent.futures.as_completed(futures)]
        
        # Verify all workers completed successfully
        self.assertEqual(len(results), 5)
        for worker_results in results:
            self.assertEqual(len(worker_results), 10)
            for crowding_score in worker_results:
                self.assertGreaterEqual(crowding_score, 0.0)
                self.assertLessEqual(crowding_score, 1.0)
    
    def test_memory_efficiency(self):
        """Test memory efficiency of the system"""
        import psutil
        import os
        
        process = psutil.Process(os.getpid())
        initial_memory = process.memory_info().rss / (1024 * 1024)  # MB
        
        # Add many models
        for i in range(500):
            self.correlation_analyzer.update_model_metrics(
                model_id=f'memory_test_{i}',
                strategy_type='momentum',
                asset='AAPL',
                horizon='1h',
                pnl=np.random.normal(0, 0.02),
                factor_exposures={'momentum': np.random.uniform(-1, 1)}
            )
        
        # Calculate correlations
        for i in range(0, 100, 2):
            model1 = f'memory_test_{i}'
            model2 = f'memory_test_{i+1}'
            self.correlation_analyzer.calculate_model_correlation(model1, model2)
        
        final_memory = process.memory_info().rss / (1024 * 1024)  # MB
        memory_increase = final_memory - initial_memory
        
        # Memory usage should be reasonable
        self.assertLess(memory_increase, 200)  # Less than 200MB increase
        
        print(f"Memory usage: {initial_memory:.1f}MB -> {final_memory:.1f}MB (+{memory_increase:.1f}MB)")
    
    def test_accuracy_of_correlation_calculations(self):
        """Test accuracy of correlation calculations"""
        # Create models with known correlation patterns
        # Model 1 and 2 should be highly correlated (same strategy and asset)
        # Model 3 should be less correlated (different strategy)
        
        correlated_pnl = np.random.normal(0.001, 0.02, 100)
        uncorrelated_pnl = np.random.normal(0.001, 0.02, 100)
        
        self.correlation_analyzer.update_model_metrics(
            model_id='correlated_1',
            strategy_type='momentum',
            asset='AAPL',
            horizon='1h',
            pnl=correlated_pnl[0],
            factor_exposures={'momentum': 0.8}
        )
        
        self.correlation_analyzer.update_model_metrics(
            model_id='correlated_2',
            strategy_type='trend',  # Similar strategy
            asset='AAPL',  # Same asset
            horizon='1h',
            pnl=correlated_pnl[0] + np.random.normal(0, 0.005),  # Add small noise
            factor_exposures={'momentum': 0.7}
        )
        
        self.correlation_analyzer.update_model_metrics(
            model_id='uncorrelated',
            strategy_type='volatility',  # Different strategy
            asset='MSFT',  # Different asset
            horizon='1d',  # Different horizon
            pnl=uncorrelated_pnl[0],
            factor_exposures={'volatility': 0.8}
        )
        
        # Calculate correlations
        corr_1_2 = self.correlation_analyzer.calculate_model_correlation('correlated_1', 'correlated_2')
        corr_1_uncorr = self.correlation_analyzer.calculate_model_correlation('correlated_1', 'uncorrelated')
        
        # Correlated models should have higher correlation
        self.assertGreater(corr_1_2.combined_correlation, corr_1_uncorr.combined_correlation)
        self.assertGreater(corr_1_2.asset_overlap, corr_1_uncorr.asset_overlap)
    
    def test_edge_cases_and_error_handling(self):
        """Test edge cases and error handling"""
        # Test with empty data
        empty_correlation = self.correlation_analyzer.calculate_model_correlation('nonexistent1', 'nonexistent2')
        self.assertEqual(empty_correlation.combined_correlation, 0.0)
        
        # Test with single model
        self.correlation_analyzer.update_model_metrics(
            model_id='single_model',
            strategy_type='momentum',
            asset='AAPL',
            horizon='1h'
        )
        
        single_crowding = self.correlation_analyzer.detect_crowding('single_model')
        self.assertIsInstance(single_crowding, CrowdingMetrics)
        self.assertEqual(single_crowding.overall_crowding, 0.0)
        
        # Test penalty system with empty allocations
        empty_penalties = self.penalty_system.apply_penalties_to_allocations([])
        self.assertEqual(len(empty_penalties), 0)
        
        # Test concentration controller with empty portfolio
        self.concentration_controller.update_portfolio([])
        empty_summary = self.concentration_controller.get_risk_summary()
        self.assertIn('concentration_metrics', empty_summary)


class TestSystemIntegration(unittest.TestCase):
    """Integration tests for the complete system"""
    
    def setUp(self):
        """Set up integration test environment"""
        self.constraints = CapitalConstraints(
            enable_crowding_penalties=True,
            enable_concentration_limits=True,
            max_crowding_score=0.4,
            max_concentration_risk="medium"
        )
        
        self.engine = CapitalAllocationEngine(self.constraints)
        self.monitoring_system = RiskMonitoringSystem()
        
        # Register systems
        self.monitoring_system.register_systems(
            self.engine.enhanced_correlation_analyzer,
            self.engine.crowding_penalty_system,
            self.engine.concentration_controller
        )
    
    def test_end_to_end_allocation_workflow(self):
        """Test complete allocation workflow"""
        # Create realistic test alerts
        test_alerts = [
            {
                'strategy': 'momentum',
                'symbol': 'AAPL',
                'expected_z': 2.1,
                'confidence': 0.92,
                'horizon': '1h'
            },
            {
                'strategy': 'momentum',
                'symbol': 'MSFT',
                'expected_z': 1.8,
                'confidence': 0.88,
                'horizon': '1h'
            },
            {
                'strategy': 'mean_reversion',
                'symbol': 'GOOGL',
                'expected_z': 1.5,
                'confidence': 0.85,
                'horizon': '4h'
            },
            {
                'strategy': 'volatility',
                'symbol': 'JPM',
                'expected_z': 1.3,
                'confidence': 0.80,
                'horizon': '1d'
            }
        ]
        
        # Run allocation
        start_time = time.time()
        targets = self.engine.allocate_capital(test_alerts)
        allocation_time = time.time() - start_time
        
        # Verify results
        self.assertIsInstance(targets, list)
        self.assertLess(allocation_time, 2.0)  # Should complete within 2 seconds
        
        # Get comprehensive summary
        summary = self.engine.get_allocation_summary(targets)
        
        # Verify enhanced metrics are present
        self.assertIn('enhanced_correlation_risk', summary)
        self.assertIn('penalty_statistics', summary)
        self.assertIn('concentration_risk', summary)
        self.assertIn('diversification_score', summary)
        
        # Verify penalty tracking
        total_penalty = sum(
            t.penalty_breakdown.get('total', 0) 
            for t in targets 
            if t.penalty_breakdown
        )
        self.assertGreaterEqual(total_penalty, 0.0)
        
        print(f"End-to-end test: {len(targets)} allocations in {allocation_time:.3f}s")
        print(f"Total penalty applied: {total_penalty:.3f}")
        print(f"Diversification score: {summary['diversification_score']:.3f}")
    
    def test_system_stability_under_stress(self):
        """Test system stability under stress conditions"""
        # Generate large number of alerts
        stress_alerts = []
        for i in range(50):
            stress_alerts.append({
                'strategy': np.random.choice(['momentum', 'mean_reversion', 'volatility', 'macro']),
                'symbol': np.random.choice(['AAPL', 'MSFT', 'GOOGL', 'JPM', 'XOM']),
                'expected_z': np.random.uniform(1.0, 2.5),
                'confidence': np.random.uniform(0.7, 0.95),
                'horizon': np.random.choice(['5m', '1h', '4h', '1d'])
            })
        
        # Run multiple allocation cycles
        allocation_times = []
        for cycle in range(5):
            start_time = time.time()
            targets = self.engine.allocate_capital(stress_alerts)
            allocation_time = time.time() - start_time
            allocation_times.append(allocation_time)
            
            # Verify results are consistent
            self.assertIsInstance(targets, list)
            for target in targets:
                self.assertGreaterEqual(target.weight, 0.0)
                self.assertLessEqual(target.weight, self.constraints.max_position_size)
        
        # Check performance consistency
        avg_time = np.mean(allocation_times)
        max_time = np.max(allocation_times)
        
        self.assertLess(avg_time, 3.0)  # Average should be under 3 seconds
        self.assertLess(max_time, 5.0)  # Max should be under 5 seconds
        
        print(f"Stress test: 5 cycles with 50 alerts each")
        print(f"Average time: {avg_time:.3f}s, Max time: {max_time:.3f}s")


if __name__ == '__main__':
    # Run tests
    unittest.main(verbosity=2)
