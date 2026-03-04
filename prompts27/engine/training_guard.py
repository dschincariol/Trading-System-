"""
Training guard stub.
Prevents runtime import failure during refactor.
"""

def is_training_enabled():
    return False

def get_training_state():
    return {
        "enabled": False,
        "reason": "training_guard_stub"
    }

def get_training_status():
    """Dashboard-readable status bundle (mode, reason, updated_ts_ms)"""
    return {
        "mode": "disabled",
        "reason": "training_guard_stub",
        "allowed": False,
        "updated_ts_ms": 0
    }

def set_training_mode(mode, reason=None):
    """Stub implementation"""
    pass