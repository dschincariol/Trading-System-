# engine/runtime/execution_barrier.py
from dataclasses import dataclass
from typing import Dict, Any, Optional


@dataclass(frozen=True)
class ExecBarrierDecision:
    allowed: bool
    reason: str
    detail: Dict[str, Any]

def execution_barrier_decide(
    system_state: Dict[str, Any],
    kill_switches: Optional[Dict[str, Any]],
    execution_degraded: bool,
    portfolio_risk_gate: Optional[Dict[str, Any]] = None,
) -> ExecBarrierDecision:
    # Fail-closed defaults
    if not system_state or not isinstance(system_state, dict):
        return ExecBarrierDecision(False, "system_state_missing", {})

    st = str(system_state.get("state") or "")
    ok = bool(system_state.get("ok", False))

    if not ok and st not in ("DEGRADED",):
        return ExecBarrierDecision(False, "system_state_not_ok", {"state": st})

    if st not in ("LIVE", "DEGRADED"):
        return ExecBarrierDecision(False, "system_state_not_live", {"state": st})

    if kill_switches and isinstance(kill_switches, dict):
        # If ANY kill switch is active => block
        active = []
        for k, v in kill_switches.items():
            if isinstance(v, dict) and v.get("active"):
                active.append(k)
            elif v is True:
                active.append(k)
        if active:
            return ExecBarrierDecision(False, "kill_switch_active", {"active": active})

    if execution_degraded:
        return ExecBarrierDecision(False, "execution_degraded", {})

    if portfolio_risk_gate and isinstance(portfolio_risk_gate, dict):
        if portfolio_risk_gate.get("blocked"):
            return ExecBarrierDecision(False, "portfolio_risk_gate_block", portfolio_risk_gate)

    return ExecBarrierDecision(True, "ok", {"state": st})

# -------------------------------------------------------------------
# Snapshot adapter for health / API layer
# -------------------------------------------------------------------
# -------------------------------------------------------------------
# Snapshot adapter for health / API layer
# -------------------------------------------------------------------

def execution_gate_snapshot(
    system_state: Optional[Dict[str, Any]] = None,
    kill_switches: Optional[Dict[str, Any]] = None,
    execution_degraded: bool = False,
    portfolio_risk_gate: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Backward-compatible snapshot wrapper expected by API layer.
    Must never raise.
    """

    try:
        # If system_state not provided, allow warm-up phase
        if not system_state:
            return {
                "allowed": False,
                "reason": "warming_up",
                "detail": {},
                "ok": True,
            }

        decision = execution_barrier_decide(
            system_state=system_state,
            kill_switches=kill_switches,
            execution_degraded=execution_degraded,
            portfolio_risk_gate=portfolio_risk_gate,
        )

        return {
            "allowed": bool(decision.allowed),
            "reason": decision.reason,
            "detail": decision.detail,
            "ok": True,
        }

    except Exception as e:
        return {
            "allowed": False,
            "reason": f"execution_barrier_error: {e}",
            "detail": {},
            "ok": True,
        }