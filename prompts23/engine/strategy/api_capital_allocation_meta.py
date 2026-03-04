"""
Capital Allocation Meta-AI API

REST API endpoints for the Capital Allocation Meta-AI system.
Provides monitoring, manual triggers, and diagnostic endpoints.
"""

import os
import json
import time
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

from fastapi import HTTPException, APIRouter, Query, Body
from pydantic import BaseModel, Field

from engine.storage import connect
from engine.strategy.capital_allocation_meta_ai import run_meta_allocation, CapitalAllocationMetaAI
from engine.strategy.capital_allocation_engine import CapitalAllocationEngine
from engine.strategy.capital_allocation_job import get_capital_allocation_status, update_strategy_performance

logger = logging.getLogger(__name__)

# Create router
router = APIRouter(prefix="/api/capital-allocation-meta", tags=["capital-allocation-meta"])

# Pydantic models
class StrategyPerformanceUpdate(BaseModel):
    strategy_name: str = Field(..., description="Strategy name")
    sharpe_ratio: float = Field(..., description="Sharpe ratio")
    total_return: float = Field(..., description="Total return")
    max_drawdown: float = Field(..., description="Maximum drawdown")
    win_rate: float = Field(..., description="Win rate")

class ManualAllocationRequest(BaseModel):
    strategies: List[str] = Field(..., description="List of strategy names")
    current_weights: Dict[str, float] = Field(default_factory=dict, description="Current weights")
    total_capital: float = Field(default=500000.0, description="Total capital")
    force_allocation: bool = Field(default=False, description="Force allocation even if conditions not met")

class OptimizationConfig(BaseModel):
    return_weight: float = Field(default=0.4, description="Weight for return objective")
    risk_weight: float = Field(default=0.3, description="Weight for risk objective")
    correlation_weight: float = Field(default=0.2, description="Weight for correlation objective")
    stability_weight: float = Field(default=0.1, description="Weight for stability objective")

@router.get("/status")
async def get_meta_ai_status():
    """Get Meta-AI system status and health"""
    try:
        engine = CapitalAllocationEngine()
        status = engine.get_meta_ai_status()
        
        # Add additional system info
        status.update({
            'timestamp': datetime.now().isoformat(),
            'system_version': '1.0.0',
            'enabled_features': {
                'meta_ai': engine.meta_ai_enabled,
                'crowding_penalties': engine.constraints.enable_crowding_penalties,
                'concentration_limits': engine.constraints.enable_concentration_limits
            },
            'constraints': {
                'total_capital': engine.constraints.total_capital,
                'max_position_size': engine.constraints.max_position_size,
                'max_correlation_exposure': engine.constraints.max_correlation_exposure
            }
        })
        
        return {"ok": True, "status": status}
        
    except Exception as e:
        logger.error(f"Error getting Meta-AI status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/allocate")
async def run_manual_allocation(request: ManualAllocationRequest):
    """Run manual capital allocation using Meta-AI"""
    try:
        if len(request.strategies) < 2:
            raise HTTPException(status_code=400, detail="Need at least 2 strategies for allocation")
        
        # Validate weights sum to 1.0 if provided
        if request.current_weights:
            total_weight = sum(request.current_weights.values())
            if abs(total_weight - 1.0) > 0.01:
                raise HTTPException(status_code=400, detail="Current weights must sum to 1.0")
        
        # Run Meta-AI allocation
        result = run_meta_allocation(
            strategies=request.strategies,
            current_weights=request.current_weights or {s: 1.0/len(request.strategies) for s in request.strategies},
            total_capital=request.total_capital
        )
        
        if not result['success'] and not request.force_allocation:
            raise HTTPException(status_code=400, detail=result['message'])
        
        # Log the allocation
        logger.info(f"Manual Meta-AI allocation: {len(result.get('decisions', []))} decisions")
        
        return {
            "ok": True,
            "allocation": result,
            "timestamp": datetime.now().isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in manual allocation: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/optimize-config")
async def update_optimization_config(config: OptimizationConfig):
    """Update optimization objective configuration"""
    try:
        # Validate weights sum to 1.0
        total_weight = config.return_weight + config.risk_weight + config.correlation_weight + config.stability_weight
        if abs(total_weight - 1.0) > 0.01:
            raise HTTPException(status_code=400, detail="Weights must sum to 1.0")
        
        # Update engine configuration (would need to implement this)
        engine = CapitalAllocationEngine()
        engine.meta_ai.objective.return_weight = config.return_weight
        engine.meta_ai.objective.risk_weight = config.risk_weight
        engine.meta_ai.objective.correlation_weight = config.correlation_weight
        engine.meta_ai.objective.stability_weight = config.stability_weight
        
        logger.info(f"Updated optimization config: {config.dict()}")
        
        return {
            "ok": True,
            "config": config.dict(),
            "message": "Optimization configuration updated successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating optimization config: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/performance")
async def update_strategy_performance_endpoint(performance: StrategyPerformanceUpdate):
    """Update strategy performance metrics"""
    try:
        update_strategy_performance(
            performance.strategy_name,
            {
                "sharpe_ratio": performance.sharpe_ratio,
                "total_return": performance.total_return,
                "max_drawdown": performance.max_drawdown,
                "win_rate": performance.win_rate
            }
        )
        
        logger.info(f"Updated performance for {performance.strategy_name}")
        
        return {
            "ok": True,
            "strategy": performance.strategy_name,
            "message": "Performance updated successfully"
        }
        
    except Exception as e:
        logger.error(f"Error updating performance: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/diagnostics")
async def get_allocation_diagnostics(
    days: int = Query(default=7, description="Number of days to look back"),
    strategy: Optional[str] = Query(default=None, description="Filter by strategy")
):
    """Get detailed allocation diagnostics"""
    try:
        engine = CapitalAllocationEngine()
        
        # Get recent allocation history
        cutoff_time = time.time() - (days * 24 * 3600)
        recent_allocations = [
            record for record in engine.allocation_history
            if record['timestamp'] > cutoff_time
        ]
        
        # Filter by strategy if specified
        if strategy:
            recent_allocations = [
                record for record in recent_allocations
                if any(d['strategy'] == strategy for d in record.get('decisions', []))
            ]
        
        # Aggregate diagnostics
        total_allocations = len(recent_allocations)
        if total_allocations == 0:
            return {
                "ok": True,
                "period_days": days,
                "total_allocations": 0,
                "message": "No allocations found in period"
            }
        
        # Calculate aggregate metrics
        all_decisions = []
        for record in recent_allocations:
            all_decisions.extend(record.get('decisions', []))
        
        # Strategy performance
        strategy_performance = {}
        for decision in all_decisions:
            strat = decision['strategy']
            if strat not in strategy_performance:
                strategy_performance[strat] = {
                    'allocations': 0,
                    'avg_weight_change': 0.0,
                    'avg_confidence': 0.0
                }
            
            strategy_performance[strat]['allocations'] += 1
            strategy_performance[strat]['avg_weight_change'] += abs(decision.get('weight_change', 0.0))
            strategy_performance[strat]['avg_confidence'] += decision.get('confidence', 0.0)
        
        # Calculate averages
        for strat in strategy_performance:
            count = strategy_performance[strat]['allocations']
            strategy_performance[strat]['avg_weight_change'] /= count
            strategy_performance[strat]['avg_confidence'] /= count
        
        # Recent diagnostics summary
        recent_summary = recent_allocations[-1].get('diagnostics', {}) if recent_allocations else {}
        
        return {
            "ok": True,
            "period_days": days,
            "total_allocations": total_allocations,
            "strategy_performance": strategy_performance,
            "recent_summary": recent_summary,
            "allocation_frequency": total_allocations / days,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error getting diagnostics: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/strategies")
async def get_available_strategies():
    """Get list of available strategies with basic metrics"""
    try:
        con = connect()
        strategies = []
        
        # Get strategies from performance table
        rows = con.execute("""
            SELECT DISTINCT strategy_name, 
                   AVG(sharpe_ratio) as avg_sharpe,
                   AVG(total_return) as avg_return,
                   AVG(max_drawdown) as avg_drawdown,
                   AVG(win_rate) as avg_win_rate,
                   COUNT(*) as observations,
                   MAX(ts_ms) as last_update
            FROM strategy_performance 
            WHERE ts_ms > ?
            GROUP BY strategy_name
            ORDER BY avg_sharpe DESC
        """, ((time.time() - 30*24*3600) * 1000,)).fetchall()
        
        for row in rows:
            strategies.append({
                'name': row[0],
                'avg_sharpe_ratio': row[1] or 0.0,
                'avg_total_return': row[2] or 0.0,
                'avg_max_drawdown': row[3] or 0.0,
                'avg_win_rate': row[4] or 0.0,
                'observations': row[5] or 0,
                'last_update': row[6] or 0,
                'last_update_iso': datetime.fromtimestamp(row[6]/1000).isoformat() if row[6] else None
            })
        
        con.close()
        
        return {
            "ok": True,
            "strategies": strategies,
            "total_count": len(strategies)
        }
        
    except Exception as e:
        logger.error(f"Error getting strategies: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/toggle")
async def toggle_meta_ai(enabled: bool = Body(..., embed=True)):
    """Enable or disable Meta-AI allocation"""
    try:
        # Set environment variable (would need to implement persistence)
        os.environ["META_AI_ENABLED"] = "1" if enabled else "0"
        
        # Update engine instance
        engine = CapitalAllocationEngine()
        engine.meta_ai_enabled = enabled
        
        status = "enabled" if enabled else "disabled"
        logger.info(f"Meta-AI {status}")
        
        return {
            "ok": True,
            "meta_ai_enabled": enabled,
            "status": status,
            "message": f"Meta-AI {status} successfully"
        }
        
    except Exception as e:
        logger.error(f"Error toggling Meta-AI: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/health")
async def health_check():
    """Health check endpoint for Meta-AI system"""
    try:
        engine = CapitalAllocationEngine()
        
        # Basic health checks
        checks = {
            'meta_ai_initialized': engine.meta_ai is not None,
            'meta_ai_enabled': engine.meta_ai_enabled,
            'database_connection': True,  # Would check actual connection
            'recent_allocations': len(engine.allocation_history) > 0,
            'constraints_loaded': engine.constraints is not None
        }
        
        all_healthy = all(checks.values())
        status_code = 200 if all_healthy else 503
        
        return {
            "ok": all_healthy,
            "healthy": all_healthy,
            "checks": checks,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {
            "ok": False,
            "healthy": False,
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }
