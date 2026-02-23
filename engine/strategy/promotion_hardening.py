# dev_core/promotion_hardening.py
import json
import os
import time
from typing import Any, Callable, Dict, Optional

from engine.storage import connect, init_db
from engine.model_registry import (
    get_stage_latest,
    promote_to_champion,
    rollback_champion,
)
from engine.promotion_audit import audit


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_health_snapshot(health_fn: Optional[Callable[[], Dict[str, Any]]]) -> Dict[str, Any]:
    if not health_fn:
        return {"ok": True, "note": "no_health_fn"}
    try:
        h = health_fn()
        if isinstance(h, dict):
            return h
        return {"ok": True, "note": "health_fn_non_dict"}
    except Exception as e:
        return {"ok": False, "error": "health_fn_exception", "detail": repr(e)}


def promote_with_snapshot_and_watch(
    model_name: str,
    model_kind: str,
    model_ts_ms: int,
    *,
    key: Optional[str] = None,
    actor: str = "auto",
    health_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    watch_seconds: Optional[float] = None,
    watch_interval_s: Optional[float] = None,
    max_bad: Optional[int] = None,
    extra_reason: Optional[Dict[str, Any]] = None,
) -> bool:

    """
    Hardened promotion:
      1) snapshot prev champion
      2) promote
      3) watch health for a bounded window
      4) rollback if health degrades repeatedly

    Returns True if promotion stayed, False if rolled back or failed.

    ENV defaults:
      PROMOTE_WATCH_SECONDS=60
      PROMOTE_WATCH_INTERVAL=5
      PROMOTE_WATCH_MAX_BAD=2
    """
    k = str(key) if key is not None else None

    ws = float(watch_seconds if watch_seconds is not None else os.environ.get("PROMOTE_WATCH_SECONDS", "60"))
    wi = float(watch_interval_s if watch_interval_s is not None else os.environ.get("PROMOTE_WATCH_INTERVAL", "5"))
    mb = int(max_bad if max_bad is not None else os.environ.get("PROMOTE_WATCH_MAX_BAD", "2"))

    prev = get_stage_latest(str(model_name), "champion", key=k)
    prev_kind = prev.get("model_kind") if prev else None
    prev_ts = prev.get("model_ts_ms") if prev else None

    before_health = _safe_health_snapshot(health_fn)

    # Promote
    try:
        promote_to_champion(str(model_name), str(model_kind), int(model_ts_ms), key=k)
    except Exception as e:
        audit(
            actor=str(actor),
            action="block",
            model_name=str(model_name),
            to_kind=str(model_kind),
            to_ts_ms=int(model_ts_ms),
            regime=(k if k is not None else "global"),
            reason={
                "error": "promote_exception",
                "detail": repr(e),
                "key": k,
                "prev_kind": prev_kind,
                "prev_ts_ms": prev_ts,
                "before_health": before_health,
                "extra": extra_reason or {},
            },
        )
        return False

    audit(
        actor=str(actor),
        action="promote",
        model_name=str(model_name),
        from_kind=prev_kind,
        from_ts_ms=prev_ts,
        to_kind=str(model_kind),
        to_ts_ms=int(model_ts_ms),
        regime=(k if k is not None else "global"),
        reason={
            "key": k,
            "before_health": before_health,
            "extra": extra_reason or {},
        },
    )

    # Watch
    bad = 0
    start = time.time()
    last_health = None

    while (time.time() - start) < ws:
        time.sleep(max(0.25, wi))
        last_health = _safe_health_snapshot(health_fn)

        # Treat missing/failed health checks as "bad" (fail-closed)
        ok = bool(last_health.get("ok", False))
        if ok:
            continue

        bad += 1
        if bad < mb:
            continue

        # Rollback
        try:
            rollback_champion(str(model_name), key=k)
        except Exception as e:
            audit(
                actor=str(actor),
                action="block",
                model_name=str(model_name),
                regime=(k if k is not None else "global"),
                reason={
                    "error": "rollback_exception",
                    "detail": repr(e),
                    "key": k,
                    "last_health": last_health,
                    "bad": bad,
                },
            )
            return False

        audit(
            actor=str(actor),
            action="rollback",
            model_name=str(model_name),
            from_kind=str(model_kind),
            from_ts_ms=int(model_ts_ms),
            to_kind=prev_kind,
            to_ts_ms=prev_ts,
            regime=(k if k is not None else "global"),
            reason={
                "key": k,
                "bad": bad,
                "last_health": last_health,
                "prev_kind": prev_kind,
                "prev_ts_ms": prev_ts,
                "extra": extra_reason or {},
            },
        )
        return False

    # Stayed promoted
    audit(
        actor=str(actor),
        action="promote_ok",
        model_name=str(model_name),
        to_kind=str(model_kind),
        to_ts_ms=int(model_ts_ms),
        regime=(k if k is not None else "global"),
        reason={
            "key": k,
            "watch_seconds": ws,
            "bad": bad,
            "last_health": last_health,
            "extra": extra_reason or {},
        },
    )
    return True


def start_watch(
    *,
    model_name: str,
    regime: str,
    from_kind: Optional[str],
    from_ts_ms: Optional[int],
    to_kind: str,
    to_ts_ms: int,
    baseline_metrics: Dict[str, Any],
    watch_s: int,
    note: Optional[str] = None,
    con=None,
) -> int:
    init_db()
    owns = False
    if con is None:
        con = connect()
        owns = True
    try:
        now = _now_ms()
        wid = con.execute(
            """
            INSERT INTO model_post_promo_watch(
              ts_ms, model_name, regime,
              from_model_kind, from_model_ts_ms,
              to_model_kind, to_model_ts_ms,
              watch_until_ts_ms, baseline_metrics_json,
              status, last_eval_ts_ms, note
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(now),
                str(model_name),
                str(regime),
                (str(from_kind) if from_kind else None),
                (int(from_ts_ms) if from_ts_ms is not None else None),
                str(to_kind),
                int(to_ts_ms),
                int(now + int(watch_s) * 1000),
                json.dumps(baseline_metrics or {}, separators=(",", ":"), sort_keys=True),
                "active",
                None,
                (str(note) if note else None),
            ),
        ).lastrowid
        con.commit()
        return int(wid)
    finally:
        if owns:
            con.close()


def close_watch(watch_id: int, status: str, note: Optional[str] = None) -> None:
    init_db()
    con = connect()
    try:
        

        con.execute(
            """
            UPDATE model_post_promo_watch
            SET status=?, note=COALESCE(note,'') || ?, last_eval_ts_ms=?
            WHERE id=?
            """,
            (
                str(status),
                (("\n" + str(note)) if note else ""),
                _now_ms(),
                int(watch_id),
            ),
        )
        con.commit()
    finally:
        con.close()

def auto_rollback(
    *,
    actor: str,
    model_name: str,
    regime: str,
    watch_id: int,
    reason: Dict[str, Any],
) -> Optional[Dict[str, Any]]:

    init_db()

    ch_before = get_stage_latest(str(model_name), "champion", key=str(regime))
    rb = rollback_champion(str(model_name), key=str(regime))
    if not rb:
        return None

    audit(
        actor=str(actor),
        action="auto_rollback",
        model_name=str(model_name),
        from_kind=(ch_before.get("model_kind") if ch_before else None),
        from_ts_ms=(ch_before.get("model_ts_ms") if ch_before else None),
        to_kind=rb.get("model_kind"),
        to_ts_ms=rb.get("model_ts_ms"),
        regime=str(regime),
        reason={"watch_id": int(watch_id), **(reason or {})},
    )
    close_watch(int(watch_id), "rolled_back", note="auto rollback executed")
    return rb


def manual_rollback(
    *,
    actor: str,
    model_name: str,
    regime: str,
    reason: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    init_db()
    ch_before = get_stage_latest(str(model_name), "champion", key=str(regime))
    rb = rollback_champion(str(model_name), key=str(regime))
    if not rb:
        return None

    audit(
        actor=str(actor),
        action="manual_rollback",
        model_name=str(model_name),
        from_kind=(ch_before.get("model_kind") if ch_before else None),
        from_ts_ms=(ch_before.get("model_ts_ms") if ch_before else None),
        to_kind=rb.get("model_kind"),
        to_ts_ms=rb.get("model_ts_ms"),
        regime=str(regime),
        reason=reason or {},
    )
    return rb


def promote_with_snapshot_and_db_watch(
    model_name: str,
    model_kind: str,
    model_ts_ms: int,
    *,
    key: Optional[str] = None,
    actor: str = "auto",
    watch_s: Optional[int] = None,
    extra_reason: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    DB-backed post-promotion watch:
      - snapshot prev champion metrics
      - promote
      - write watch row for monitor job
      - audit promote + watch_start

    Returns True if promoted successfully (watch is evaluated asynchronously).
    """
    init_db()
    reg = str(key) if key is not None else "global"
    ws = int(watch_s if watch_s is not None else int(os.environ.get("POST_PROMO_WATCH_S", "7200")))

    con = connect()
    try:
        prev = get_stage_latest(str(model_name), "champion", key=str(reg))
        prev_kind = prev.get("model_kind") if prev else None
        prev_ts = prev.get("model_ts_ms") if prev else None
        baseline = (prev.get("metrics") if prev else {}) or {}

        try:
            promote_to_champion(str(model_name), str(model_kind), int(model_ts_ms), key=str(reg))
        except Exception as e:
            audit(
                actor=str(actor),
                action="block",
                model_name=str(model_name),
                to_kind=str(model_kind),
                to_ts_ms=int(model_ts_ms),
                regime=str(reg),
                reason={
                    "error": "promote_exception",
                    "detail": repr(e),
                    "key": str(reg),
                    "prev_kind": prev_kind,
                    "prev_ts_ms": prev_ts,
                    "extra": extra_reason or {},
                },
            )
            return False

        audit(
            actor=str(actor),
            action="promote",
            model_name=str(model_name),
            from_kind=prev_kind,
            from_ts_ms=prev_ts,
            to_kind=str(model_kind),
            to_ts_ms=int(model_ts_ms),
            regime=str(reg),
            reason={
                "key": str(reg),
                "extra": extra_reason or {},
            },
        )

        wid = start_watch(
            model_name=str(model_name),
            regime=str(reg),
            from_kind=prev_kind,
            from_ts_ms=prev_ts,
            to_kind=str(model_kind),
            to_ts_ms=int(model_ts_ms),
            baseline_metrics=baseline,
            watch_s=int(ws),
            note="db watch start",
            con=con,
        )

        audit(
            actor=str(actor),
            action="watch_start",
            model_name=str(model_name),
            from_kind=prev_kind,
            from_ts_ms=prev_ts,
            to_kind=str(model_kind),
            to_ts_ms=int(model_ts_ms),
            regime=str(reg),
            reason={"watch_id": int(wid), "watch_s": int(ws), "key": str(reg), "extra": extra_reason or {}},
        )
        return True
    finally:
        con.close()
