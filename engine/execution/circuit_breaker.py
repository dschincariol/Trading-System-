# FILE: circuit_breaker.py
# NEW FILE (CREATE)

import os
import time
from engine.execution_mode import set_execution_mode, set_execution_armed

PANIC_FILE = os.environ.get("PANIC_FILE", "panic.flag")

def check_circuit_breaker():
    if os.path.exists(PANIC_FILE):
        set_execution_armed(0, actor="circuit_breaker", reason="panic_file")
        set_execution_mode("paper", actor="circuit_breaker", reason="panic_file")
        return True
    return False
