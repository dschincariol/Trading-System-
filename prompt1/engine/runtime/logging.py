# engine/runtime/logging.py
"""
Central structured logging for trading engine.
No side effects. Safe for import anywhere.
"""

import logging
import os
import sys
import json
import time

LOG_LEVEL = os.environ.get("ENGINE_LOG_LEVEL", "INFO").upper()
LOG_JSON = os.environ.get("ENGINE_LOG_JSON", "0") == "1"

_logger = logging.getLogger("engine")
_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

_handler = logging.StreamHandler(sys.stdout)

if LOG_JSON:

    class JsonFormatter(logging.Formatter):
        def format(self, record):
            payload = {
                "ts": int(time.time() * 1000),
                "level": record.levelname,
                "msg": record.getMessage(),
                "module": record.module,
            }
            return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    _handler.setFormatter(JsonFormatter())
else:
    _handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(module)s | %(message)s"
        )
    )

if not _logger.handlers:
    _logger.addHandler(_handler)

def get_logger(name: str = None):
    if not name:
        return _logger
    return _logger.getChild(name)
