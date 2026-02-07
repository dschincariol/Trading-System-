# dev_core/rules_audit.py
"""
Persistent audit trail for rule decisions.
"""

import time
import json
from dev_core.storage import connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS rules_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  scope TEXT NOT NULL,
  reason TEXT NOT NULL,
  state TEXT NOT NULL,
  details_json TEXT
);
"""


def init_rules_audit_db():
    con = connect()
    try:
        con.execute(SCHEMA)
        con.commit()
    finally:
        con.close()


def log_rule(scope: str, reason: str, state: str, details: dict | None = None):
    con = connect()
    try:
        ok = 0 if had_error else 1

        con.execute(
            """
            INSERT INTO rules_audit(ts_ms, scope, reason, state, details_json)
            VALUES (?,?,?,?,?)
            """,
            (
                int(time.time() * 1000),
                str(scope),
                str(reason),
                str(state),
                json.dumps(details or {}, separators=(",", ":"), sort_keys=True),
            ),
        )
        con.commit()
    finally:
        con.close()
