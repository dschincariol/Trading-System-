"""
Internal access bridge.

This module isolates dashboard from direct engine.dev_core imports.
All dev_core access must flow through here.
"""

# DB
from engine.dev_core.storage import connect as db_connect
from engine.dev_core.storage import init_db

# Learning
from engine.dev_core.learning import learn_relevance_stats

# Execution mode
from engine.dev_core.execution_mode import (
    get_execution_mode,
)
