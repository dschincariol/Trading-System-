"""
Risk Constraints Configuration

Defines risk limits and constraints for the capital allocation engine.
"""

from dataclasses import dataclass
from typing import Dict, List


@dataclass 
class RiskConstraints:
    # Capital
    total_capital: float = 500000.0
    
    # Portfolio-level constraints
    max_gross_exposure: float = 1.0
    max_net_exposure: float = 0.6
    max_single_position: float = 0.2
    max_sector_exposure: float = 0.3
    max_strategy_exposure: float = 0.4
    
    # Risk management
    max_correlation_risk: float = 0.3
    max_drawdown: float = 0.15
    var_limit: float = 0.05  # 5% daily VaR
    
    # Rebalancing constraints
    max_turnover: float = 0.4
    min_rebalance_interval: int = 300  # 5 minutes
    max_rebalance_interval: int = 3600  # 1 hour
    
    # Execution constraints
    max_orders_per_minute: int = 50
    max_notional_per_minute: float = 50000.0


def get_default_constraints() -> RiskConstraints:
    """Get default risk constraints"""
    return RiskConstraints()


def get_conservative_constraints() -> RiskConstraints:
    """Get conservative risk constraints"""
    return RiskConstraints(
        max_gross_exposure=0.8,
        max_net_exposure=0.4,
        max_single_position=0.1,
        max_sector_exposure=0.2,
        max_strategy_exposure=0.3,
        max_correlation_risk=0.2,
        max_drawdown=0.1,
        var_limit=0.03,
        max_turnover=0.3,
        min_rebalance_interval=600
    )


def get_aggressive_constraints() -> RiskConstraints:
    """Get aggressive risk constraints"""
    return RiskConstraints(
        max_gross_exposure=1.2,
        max_net_exposure=0.8,
        max_single_position=0.25,
        max_sector_exposure=0.4,
        max_strategy_exposure=0.5,
        max_correlation_risk=0.4,
        max_drawdown=0.2,
        var_limit=0.07,
        max_turnover=0.6,
        min_rebalance_interval=180
    )
