# stress_test_integration.py
"""
Integration layer for adversarial stress testing with existing backtesting pipeline.

This module provides hooks to integrate stress testing into:
- Training pipeline (pre-promotion validation)
- Model governance (robustness checks)
- Continuous monitoring (automated stress tests)
"""

import os
import json
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta

from engine.storage import connect
from engine.model_registry import get_stage_latest, register_model
from engine.promotion_guard import promotion_allowed
from engine.training_guard import training_allowed
from engine.research.adversarial_scenario_generator import (
    AdversarialScenarioGenerator, 
    ScenarioType,
    ScenarioParameters
)


class StressTestIntegration:
    """Integration layer for stress testing within model pipeline"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.generator = AdversarialScenarioGenerator()
        self._ensure_integration_tables()
    
    def _ensure_integration_tables(self):
        """Create tables for integration tracking"""
        schema = """
        CREATE TABLE IF NOT EXISTS stress_test_gates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_name TEXT NOT NULL,
            model_kind TEXT NOT NULL,
            model_ts_ms INTEGER NOT NULL,
            gate_type TEXT NOT NULL,  -- 'pre_promotion', 'continuous', 'manual'
            test_batch_id TEXT NOT NULL,
            passed BOOLEAN NOT NULL,
            fragility_score REAL,
            failure_modes TEXT,  -- JSON array
            created_ts_ms INTEGER NOT NULL,
            decision TEXT NOT NULL,  -- 'promote', 'block', 'warn'
            reason TEXT,
            FOREIGN KEY (test_batch_id) REFERENCES stress_test_summary(test_batch_id)
        );
        
        CREATE TABLE IF NOT EXISTS stress_test_config (
            gate_type TEXT PRIMARY KEY,
            enabled BOOLEAN DEFAULT 1,
            pass_threshold REAL DEFAULT 0.7,
            min_scenarios INTEGER DEFAULT 10,
            scenario_types TEXT,  -- JSON array of scenario types
            severity_levels TEXT,  -- JSON array of severity levels
            updated_ts_ms INTEGER NOT NULL
        );
        
        CREATE INDEX IF NOT EXISTS idx_stress_gates_model ON stress_test_gates(model_name, model_ts_ms);
        CREATE INDEX IF NOT EXISTS idx_stress_gates_type ON stress_test_gates(gate_type);
        """
        
        con = connect()
        try:
            con.executescript(schema)
            con.commit()
            
            # Insert default configuration
            con.execute("""
                INSERT OR IGNORE INTO stress_test_config 
                (gate_type, enabled, pass_threshold, min_scenarios, scenario_types, severity_levels, updated_ts_ms)
                VALUES 
                ('pre_promotion', 1, 0.8, 15, 
                 '["market_crash", "regime_shift", "liquidity_drought", "news_shock"]',
                 '[0.6, 0.8, 0.95]', ?),
                ('continuous', 1, 0.7, 8,
                 '["market_crash", "volatility_spike", "spread_widening"]',
                 '[0.4, 0.7, 0.9]', ?),
                ('manual', 1, 0.75, 12,
                 '["market_crash", "regime_shift", "liquidity_drought", "news_shock", "flash_crash"]',
                 '[0.3, 0.6, 0.9]', ?)
            """, (int(datetime.now().timestamp() * 1000),) * 3)
            con.commit()
        finally:
            con.close()
    
    def get_gate_config(self, gate_type: str) -> Dict[str, Any]:
        """Get configuration for a specific stress test gate"""
        con = connect()
        try:
            row = con.execute("""
                SELECT enabled, pass_threshold, min_scenarios, scenario_types, severity_levels
                FROM stress_test_config WHERE gate_type=?
            """, (gate_type,)).fetchone()
            
            if not row:
                return {}
            
            enabled, pass_threshold, min_scenarios, scenario_types_json, severity_levels_json = row
            
            return {
                'enabled': bool(enabled),
                'pass_threshold': float(pass_threshold),
                'min_scenarios': int(min_scenarios),
                'scenario_types': json.loads(scenario_types_json or '[]'),
                'severity_levels': json.loads(severity_levels_json or '[]')
            }
        finally:
            con.close()
    
    def run_pre_promotion_stress_test(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int
    ) -> Dict[str, Any]:
        """Run stress tests before model promotion (gate in training pipeline)"""
        
        self.logger.info(f"Running pre-promotion stress test for {model_name}:{model_kind}@{model_ts_ms}")
        
        config = self.get_gate_config('pre_promotion')
        if not config.get('enabled', False):
            return {
                'gate_type': 'pre_promotion',
                'passed': True,
                'decision': 'promote',
                'reason': 'Stress testing disabled for this gate type'
            }
        
        # Generate scenarios based on configuration
        scenario_types = [ScenarioType(t) for t in config.get('scenario_types', [])]
        severity_levels = config.get('severity_levels', [0.6, 0.8, 0.95])
        min_scenarios = config.get('min_scenarios', 15)
        
        scenarios = self.generator.generate_scenario_suite(
            scenario_types=scenario_types,
            scenarios_per_type=max(1, min_scenarios // len(scenario_types)),
            severity_levels=severity_levels
        )
        
        # Run stress test suite
        results = self.generator.run_stress_test_suite(
            scenarios=scenarios,
            pass_threshold=config['pass_threshold']
        )
        
        # Make promotion decision
        passed = results['pass_rate'] >= config['pass_threshold']
        avg_fragility = results['avg_fragility_score']
        
        if passed:
            decision = 'promote'
            reason = f"Stress test passed: {results['pass_rate']:.1%} >= {config['pass_threshold']:.1%}"
        else:
            decision = 'block'
            reason = f"Stress test failed: {results['pass_rate']:.1%} < {config['pass_threshold']:.1%}, avg fragility {avg_fragility:.3f}"
        
        # Store gate result
        self._store_gate_result(
            model_name=model_name,
            model_kind=model_kind,
            model_ts_ms=model_ts_ms,
            gate_type='pre_promotion',
            test_batch_id=results['test_batch_id'],
            passed=passed,
            fragility_score=avg_fragility,
            failure_modes=results.get('results', [])[0].get('failure_modes', []) if results.get('results') else [],
            decision=decision,
            reason=reason
        )
        
        return {
            'gate_type': 'pre_promotion',
            'test_batch_id': results['test_batch_id'],
            'passed': passed,
            'decision': decision,
            'reason': reason,
            'pass_rate': results['pass_rate'],
            'avg_fragility': avg_fragility,
            'scenario_count': results['scenario_count']
        }
    
    def run_continuous_stress_test(self, model_name: str) -> Dict[str, Any]:
        """Run continuous stress tests on champion models"""
        
        self.logger.info(f"Running continuous stress test for {model_name}")
        
        config = self.get_gate_config('continuous')
        if not config.get('enabled', False):
            return {'gate_type': 'continuous', 'passed': True, 'reason': 'Continuous testing disabled'}
        
        # Get current champion model
        champion = get_stage_latest(model_name, 'champion')
        if not champion:
            return {'gate_type': 'continuous', 'passed': False, 'reason': 'No champion model found'}
        
        # Generate lightweight scenarios for continuous monitoring
        scenario_types = [ScenarioType(t) for t in config.get('scenario_types', [])]
        severity_levels = config.get('severity_levels', [0.4, 0.7, 0.9])
        min_scenarios = config.get('min_scenarios', 8)
        
        scenarios = self.generator.generate_scenario_suite(
            scenario_types=scenario_types,
            scenarios_per_type=max(1, min_scenarios // len(scenario_types)),
            severity_levels=severity_levels
        )
        
        # Run stress test suite
        results = self.generator.run_stress_test_suite(
            scenarios=scenarios,
            pass_threshold=config['pass_threshold']
        )
        
        # Store result for monitoring
        passed = results['pass_rate'] >= config['pass_threshold']
        self._store_gate_result(
            model_name=model_name,
            model_kind=champion.get('model_kind', 'unknown'),
            model_ts_ms=champion.get('model_ts_ms', 0),
            gate_type='continuous',
            test_batch_id=results['test_batch_id'],
            passed=passed,
            fragility_score=results['avg_fragility_score'],
            failure_modes=results.get('results', [])[0].get('failure_modes', []) if results.get('results') else [],
            decision='monitor' if passed else 'alert',
            reason=f"Continuous monitoring: {results['pass_rate']:.1%} pass rate"
        )
        
        return {
            'gate_type': 'continuous',
            'test_batch_id': results['test_batch_id'],
            'passed': passed,
            'pass_rate': results['pass_rate'],
            'avg_fragility': results['avg_fragility_score']
        }
    
    def _store_gate_result(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        gate_type: str,
        test_batch_id: str,
        passed: bool,
        fragility_score: float,
        failure_modes: List[str],
        decision: str,
        reason: str
    ):
        """Store stress test gate result in database"""
        con = connect()
        try:
            con.execute("""
                INSERT INTO stress_test_gates
                (model_name, model_kind, model_ts_ms, gate_type, test_batch_id, 
                 passed, fragility_score, failure_modes, created_ts_ms, decision, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (model_name, model_kind, model_ts_ms, gate_type, test_batch_id,
                  passed, fragility_score, json.dumps(failure_modes), 
                  int(datetime.now().timestamp() * 1000), decision, reason))
            con.commit()
        finally:
            con.close()
    
    def get_stress_test_history(
        self,
        model_name: Optional[str] = None,
        gate_type: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get stress test history for monitoring and analysis"""
        con = connect()
        try:
            query = """
                SELECT model_name, model_kind, model_ts_ms, gate_type, test_batch_id,
                       passed, fragility_score, failure_modes, created_ts_ms, decision, reason
                FROM stress_test_gates
                WHERE 1=1
            """
            params = []
            
            if model_name:
                query += " AND model_name = ?"
                params.append(model_name)
            
            if gate_type:
                query += " AND gate_type = ?"
                params.append(gate_type)
            
            query += " ORDER BY created_ts_ms DESC LIMIT ?"
            params.append(limit)
            
            rows = con.execute(query, params).fetchall()
            
            results = []
            for row in rows:
                results.append({
                    'model_name': row[0],
                    'model_kind': row[1],
                    'model_ts_ms': row[2],
                    'gate_type': row[3],
                    'test_batch_id': row[4],
                    'passed': bool(row[5]),
                    'fragility_score': float(row[6]) if row[6] else None,
                    'failure_modes': json.loads(row[7] or '[]'),
                    'created_ts_ms': row[8],
                    'decision': row[9],
                    'reason': row[10]
                })
            
            return results
        finally:
            con.close()
    
    def update_gate_config(
        self,
        gate_type: str,
        enabled: Optional[bool] = None,
        pass_threshold: Optional[float] = None,
        min_scenarios: Optional[int] = None,
        scenario_types: Optional[List[str]] = None,
        severity_levels: Optional[List[float]] = None
    ):
        """Update stress test gate configuration"""
        con = connect()
        try:
            # Get current config
            current = self.get_gate_config(gate_type)
            
            # Update fields if provided
            updates = []
            params = []
            
            if enabled is not None:
                updates.append("enabled = ?")
                params.append(int(enabled))
            else:
                params.append(current.get('enabled', 1))
            
            if pass_threshold is not None:
                updates.append("pass_threshold = ?")
                params.append(float(pass_threshold))
            else:
                params.append(current.get('pass_threshold', 0.7))
            
            if min_scenarios is not None:
                updates.append("min_scenarios = ?")
                params.append(int(min_scenarios))
            else:
                params.append(current.get('min_scenarios', 10))
            
            if scenario_types is not None:
                updates.append("scenario_types = ?")
                params.append(json.dumps(scenario_types))
            else:
                params.append(json.dumps(current.get('scenario_types', [])))
            
            if severity_levels is not None:
                updates.append("severity_levels = ?")
                params.append(json.dumps(severity_levels))
            else:
                params.append(json.dumps(current.get('severity_levels', [])))
            
            params.append(int(datetime.now().timestamp() * 1000))
            params.append(gate_type)
            
            if updates:
                query = f"""
                    UPDATE stress_test_config 
                    SET {', '.join(updates)}, updated_ts_ms = ?
                    WHERE gate_type = ?
                """
                con.execute(query, params)
                con.commit()
                
                self.logger.info(f"Updated {gate_type} gate configuration")
        finally:
            con.close()


# Integration hooks for existing pipeline

def stress_test_promotion_guard(
    model_name: str,
    model_kind: str,
    model_ts_ms: int
) -> Dict[str, Any]:
    """
    Hook for integration into model promotion pipeline.
    Call this before promoting a model to champion.
    """
    integration = StressTestIntegration()
    return integration.run_pre_promotion_stress_test(
        model_name=model_name,
        model_kind=model_kind,
        model_ts_ms=model_ts_ms
    )


def stress_test_training_guard() -> bool:
    """
    Hook for integration into training pipeline.
    Returns True if training should be allowed based on recent stress test results.
    """
    integration = StressTestIntegration()
    
    # Check recent continuous stress test results
    recent_results = integration.get_stress_test_history(
        gate_type='continuous',
        limit=5
    )
    
    if not recent_results:
        return True  # No recent tests, allow training
    
    # Block training if recent stress tests show high fragility
    avg_fragility = sum(r.get('fragility_score', 0) for r in recent_results) / len(recent_results)
    
    if avg_fragility > 0.8:  # High fragility threshold
        logging.warning(f"Training blocked due to high stress test fragility: {avg_fragility:.3f}")
        return False
    
    return True


def run_scheduled_stress_tests():
    """
    Hook for scheduled execution (e.g., cron job).
    Runs continuous stress tests on all active models.
    """
    integration = StressTestIntegration()
    
    # Get all models with recent activity
    con = connect()
    try:
        rows = con.execute("""
            SELECT DISTINCT model_name 
            FROM model_registry 
            WHERE stage = 'champion' 
            AND ts_ms > ?
            ORDER BY model_name
        """, (int((datetime.now() - timedelta(days=7)).timestamp() * 1000),)).fetchall()
        
        model_names = [row[0] for row in rows]
    finally:
        con.close()
    
    results = {}
    for model_name in model_names:
        try:
            result = integration.run_continuous_stress_test(model_name)
            results[model_name] = result
            logging.info(f"Continuous stress test for {model_name}: {result['decision']}")
        except Exception as e:
            logging.error(f"Failed stress test for {model_name}: {e}")
            results[model_name] = {'error': str(e)}
    
    return results


# CLI interface
def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Stress Test Integration CLI')
    parser.add_argument('--gate-type', choices=['pre_promotion', 'continuous', 'manual'], 
                       help='Gate type to run')
    parser.add_argument('--model-name', type=str, help='Model name to test')
    parser.add_argument('--model-kind', type=str, help='Model kind to test')
    parser.add_argument('--model-ts-ms', type=int, help='Model timestamp to test')
    parser.add_argument('--config', action='store_true', help='Show current configuration')
    parser.add_argument('--update-config', action='store_true', help='Update configuration')
    parser.add_argument('--history', action='store_true', help='Show stress test history')
    parser.add_argument('--scheduled', action='store_true', help='Run scheduled stress tests')
    
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO)
    integration = StressTestIntegration()
    
    if args.config:
        for gate_type in ['pre_promotion', 'continuous', 'manual']:
            config = integration.get_gate_config(gate_type)
            print(f"\n{gate_type.upper()} Gate Configuration:")
            for key, value in config.items():
                print(f"  {key}: {value}")
        return
    
    if args.update_config:
        # Example: update configuration
        integration.update_gate_config(
            gate_type='pre_promotion',
            pass_threshold=0.85,
            min_scenarios=20
        )
        print("Updated pre_promotion gate configuration")
        return
    
    if args.history:
        history = integration.get_stress_test_history(
            model_name=args.model_name,
            gate_type=args.gate_type,
            limit=20
        )
        
        print(f"\nStress Test History:")
        print(f"{'Model':<20} {'Gate':<15} {'Passed':<8} {'Fragility':<10} {'Decision':<10} {'Date'}")
        print("-" * 80)
        
        for result in history:
            date = datetime.fromtimestamp(result['created_ts_ms']/1000).strftime('%Y-%m-%d')
            print(f"{result['model_name']:<20} {result['gate_type']:<15} "
                  f"{result['passed']:<8} {result['fragility_score'] or 'N/A':<10} "
                  f"{result['decision']:<10} {date}")
        return
    
    if args.scheduled:
        results = run_scheduled_stress_tests()
        print(f"\nScheduled Stress Test Results:")
        for model, result in results.items():
            status = result.get('decision', result.get('error', 'Unknown'))
            print(f"  {model}: {status}")
        return
    
    if args.gate_type and args.model_name:
        if args.gate_type == 'pre_promotion':
            if not args.model_kind or not args.model_ts_ms:
                print("Error: pre_promotion gate requires --model-kind and --model-ts-ms")
                return
            
            result = integration.run_pre_promotion_stress_test(
                model_name=args.model_name,
                model_kind=args.model_kind,
                model_ts_ms=args.model_ts_ms
            )
        else:
            result = integration.run_continuous_stress_test(args.model_name)
        
        print(f"\nStress Test Result:")
        for key, value in result.items():
            print(f"  {key}: {value}")
        return
    
    print("Use --help for usage information")


if __name__ == "__main__":
    main()
