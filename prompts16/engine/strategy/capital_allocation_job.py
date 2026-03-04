# engine/strategy/capital_allocation_job.py
"""
Capital Allocation Job

Integrates the capital allocation engine with the job system.
Provides automated capital allocation with execution integration.
"""

import os
import time
import json
import logging
from typing import Dict, Any, Optional

from engine.storage import connect
from engine.strategy.capital_allocation_engine import run_capital_allocation, CapitalAllocationEngine
from engine.strategy.rebalancing_manager import get_rebalancing_manager, run_rebalancing_cycle
from engine.runtime.logging import get_logger

log = get_logger("capital_allocation_job")


# Job configuration
CAPITAL_ALLOCATION_INTERVAL_S = int(os.environ.get("CAPITAL_ALLOCATION_INTERVAL_S", "300"))  # 5 minutes
CAPITAL_ALLOCATION_ENABLED = os.environ.get("CAPITAL_ALLOCATION_ENABLED", "1") == "1"
CAPITAL_ALLOCATION_DRY_RUN = os.environ.get("CAPITAL_ALLOCATION_DRY_RUN", "0") == "1"


def init_capital_allocation_schema():
    """Initialize database schema for capital allocation."""
    schema = """
    CREATE TABLE IF NOT EXISTS capital_allocation_state (
        id INTEGER PRIMARY KEY,
        ts_ms INTEGER NOT NULL,
        total_capital REAL NOT NULL,
        allocated_capital REAL NOT NULL,
        allocation_count INTEGER NOT NULL,
        correlation_penalty_avg REAL NOT NULL,
        execution_capacity_used REAL NOT NULL,
        metadata TEXT
    );
    
    CREATE TABLE IF NOT EXISTS execution_capacity (
        id INTEGER PRIMARY KEY,
        date TEXT NOT NULL,
        strategy TEXT NOT NULL,
        asset TEXT NOT NULL,
        daily_capacity REAL NOT NULL,
        daily_usage REAL DEFAULT 0.0,
        UNIQUE(date, strategy, asset)
    );
    
    CREATE TABLE IF NOT EXISTS asset_returns (
        id INTEGER PRIMARY KEY,
        ts_ms INTEGER NOT NULL,
        date TEXT NOT NULL,
        symbol TEXT NOT NULL,
        daily_return REAL NOT NULL
    );
    
    CREATE TABLE IF NOT EXISTS strategy_performance (
        id INTEGER PRIMARY KEY,
        ts_ms INTEGER NOT NULL,
        strategy_name TEXT NOT NULL,
        sharpe_ratio REAL,
        total_return REAL,
        max_drawdown REAL,
        win_rate REAL
    );
    
    CREATE INDEX IF NOT EXISTS idx_capital_allocation_ts ON capital_allocation_state(ts_ms);
    CREATE INDEX IF NOT EXISTS idx_execution_capacity_date ON execution_capacity(date);
    CREATE INDEX IF NOT EXISTS idx_asset_returns_symbol_ts ON asset_returns(symbol, ts_ms);
    CREATE INDEX IF NOT EXISTS idx_strategy_performance_strategy_ts ON strategy_performance(strategy_name, ts_ms);
    """
    
    con = connect()
    try:
        con.executescript(schema)
        con.commit()
        log.info("Capital allocation schema initialized")
    except Exception as e:
        log.error(f"Failed to initialize capital allocation schema: {e}")
        raise
    finally:
        con.close()


def run_capital_allocation_job() -> Dict[str, Any]:
    """Main capital allocation job entry point."""
    if not CAPITAL_ALLOCATION_ENABLED:
        return {"ok": False, "status": "capital_allocation_disabled"}
    
    log.info("Starting capital allocation job")
    
    try:
        # Initialize schema if needed
        init_capital_allocation_schema()
        
        # Run allocation calculation
        targets = run_capital_allocation()
        
        if not targets:
            log.info("No allocation targets generated")
            return {"ok": True, "status": "no_targets", "targets": []}
        
        # Convert targets to summary
        summary = _summarize_targets(targets)
        
        # Save allocation state
        _save_allocation_state(summary)
        
        # Run rebalancing if enabled
        if not CAPITAL_ALLOCATION_DRY_RUN:
            rebalance_result = run_rebalancing_cycle(dry_run=False)
            summary["rebalance_result"] = rebalance_result
        else:
            log.info("Dry run mode - skipping execution")
            summary["rebalance_result"] = {"ok": True, "status": "dry_run"}
        
        log.info(f"Capital allocation completed: {summary['total_weight']:.2f} weight allocated")
        
        return {
            "ok": True,
            "status": "allocation_completed",
            "summary": summary,
            "targets_count": len(targets)
        }
        
    except Exception as e:
        log.error(f"Capital allocation job failed: {e}", exc_info=True)
        return {
            "ok": False,
            "status": "allocation_failed",
            "error": str(e)
        }


def _summarize_targets(targets) -> Dict[str, Any]:
    """Summarize allocation targets."""
    if not targets:
        return {
            "total_weight": 0.0,
            "strategy_allocation": {},
            "asset_allocation": {},
            "horizon_allocation": {},
            "correlation_penalty_avg": 0.0,
            "execution_capacity_used": 0.0,
            "num_positions": 0
        }
    
    strategy_weights = {}
    asset_weights = {}
    horizon_weights = {}
    total_correlation_penalty = 0.0
    total_execution_capacity = 0.0
    
    for target in targets:
        # Strategy allocation
        strategy_weights[target.strategy] = strategy_weights.get(target.strategy, 0.0) + target.weight
        
        # Asset allocation
        asset_weights[target.asset] = asset_weights.get(target.asset, 0.0) + target.weight
        
        # Horizon allocation
        horizon_weights[target.horizon] = horizon_weights.get(target.horizon, 0.0) + target.weight
        
        # Penalties and capacity
        total_correlation_penalty += target.correlation_penalty
        total_execution_capacity += target.execution_capacity_limit
    
    return {
        "total_weight": sum(t.weight for t in targets),
        "strategy_allocation": strategy_weights,
        "asset_allocation": asset_weights,
        "horizon_allocation": horizon_weights,
        "correlation_penalty_avg": total_correlation_penalty / len(targets),
        "execution_capacity_used": total_execution_capacity,
        "num_positions": len(targets)
    }


def _save_allocation_state(summary: Dict[str, Any]):
    """Save allocation state to database."""
    con = connect()
    try:
        con.execute("""
            INSERT INTO capital_allocation_state 
            (ts_ms, total_capital, allocated_capital, allocation_count, 
             correlation_penalty_avg, execution_capacity_used, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            int(time.time() * 1000),
            500000.0,  # Default capital
            summary["total_weight"] * 500000.0,
            summary["num_positions"],
            summary["correlation_penalty_avg"],
            summary["execution_capacity_used"],
            json.dumps(summary)
        ))
        con.commit()
    except Exception as e:
        log.error(f"Failed to save allocation state: {e}")
    finally:
        con.close()


def get_capital_allocation_status() -> Dict[str, Any]:
    """Get current capital allocation status."""
    con = connect()
    try:
        # Get latest allocation state
        row = con.execute("""
            SELECT ts_ms, total_capital, allocated_capital, allocation_count,
                   correlation_penalty_avg, execution_capacity_used, metadata
            FROM capital_allocation_state
            ORDER BY ts_ms DESC
            LIMIT 1
        """).fetchone()
        
        if not row:
            return {"status": "no_allocation_history"}
        
        # Get rebalancing manager status
        rebalancing_manager = get_rebalancing_manager()
        rebalancing_status = rebalancing_manager.get_rebalancing_status()
        
        return {
            "status": "active",
            "last_allocation_ts": row[0],
            "total_capital": row[1],
            "allocated_capital": row[2],
            "allocation_count": row[3],
            "correlation_penalty_avg": row[4],
            "execution_capacity_used": row[5],
            "allocation_summary": json.loads(row[6] or "{}"),
            "rebalancing_status": rebalancing_status,
            "allocation_efficiency": (row[2] / row[1]) if row[1] > 0 else 0.0
        }
        
    except Exception as e:
        log.error(f"Failed to get capital allocation status: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        con.close()


def update_strategy_performance(strategy_name: str, performance_metrics: Dict[str, float]):
    """Update strategy performance metrics."""
    con = connect()
    try:
        con.execute("""
            INSERT INTO strategy_performance 
            (ts_ms, strategy_name, sharpe_ratio, total_return, max_drawdown, win_rate)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            int(time.time() * 1000),
            strategy_name,
            performance_metrics.get("sharpe_ratio"),
            performance_metrics.get("total_return"),
            performance_metrics.get("max_drawdown"),
            performance_metrics.get("win_rate")
        ))
        con.commit()
        log.info(f"Updated performance for strategy: {strategy_name}")
    except Exception as e:
        log.error(f"Failed to update strategy performance: {e}")
    finally:
        con.close()


def update_execution_capacity(strategy: str, asset: str, usage: float, capacity: float):
    """Update execution capacity usage."""
    con = connect()
    try:
        con.execute("""
            INSERT OR REPLACE INTO execution_capacity 
            (date, strategy, asset, daily_capacity, daily_usage)
            VALUES (date('now'), ?, ?, ?, ?)
        """, (strategy, asset, capacity, usage))
        con.commit()
    except Exception as e:
        log.error(f"Failed to update execution capacity: {e}")
    finally:
        con.close()


def get_allocation_metrics(days: int = 30) -> Dict[str, Any]:
    """Get allocation performance metrics."""
    con = connect()
    try:
        since_ms = int(time.time() * 1000) - days * 86400 * 1000
        
        # Get allocation history
        rows = con.execute("""
            SELECT ts_ms, allocated_capital, allocation_count, correlation_penalty_avg
            FROM capital_allocation_state
            WHERE ts_ms >= ?
            ORDER BY ts_ms
        """, (since_ms,)).fetchall()
        
        if not rows:
            return {"status": "no_data"}
        
        # Calculate metrics
        allocations = [float(r[1]) for r in rows]
        counts = [int(r[2]) for r in rows]
        penalties = [float(r[3]) for r in rows]
        
        return {
            "status": "success",
            "period_days": days,
            "total_allocations": len(rows),
            "avg_allocated_capital": sum(allocations) / len(allocations),
            "avg_position_count": sum(counts) / len(counts),
            "avg_correlation_penalty": sum(penalties) / len(penalties),
            "max_allocated_capital": max(allocations),
            "min_allocated_capital": min(allocations),
            "allocation_stability": 1.0 - (max(allocations) - min(allocations)) / max(allocations) if max(allocations) > 0 else 0.0
        }
        
    except Exception as e:
        log.error(f"Failed to get allocation metrics: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        con.close()


# Job entry point for the job system
def capital_allocation_job_main() -> Dict[str, Any]:
    """Main entry point for the capital allocation job."""
    return run_capital_allocation_job()


if __name__ == "__main__":
    result = capital_allocation_job_main()
    print(json.dumps(result, indent=2))
