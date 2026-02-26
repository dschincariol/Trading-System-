# engine/storage.py
# Compatibility shim: older modules import engine.runtime.storage as storage
# Canonical implementation lives in engine.runtime.storage

from engine.runtime.storage import (
    connect,
    connect_ro,
    init_db,
    put_event,
    put_price,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
    get_job_checkpoint,
    put_job_checkpoint,
    close_pooled_connections,
)

__all__ = [
    "connect",
    "connect_ro",
    "init_db",
    "put_event",
    "put_price",
    "acquire_job_lock",
    "release_job_lock",
    "touch_job_lock",
    "put_job_heartbeat",
    "get_job_checkpoint",
    "put_job_checkpoint",
    "close_pooled_connections",
]
