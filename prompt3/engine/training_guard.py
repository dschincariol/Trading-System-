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