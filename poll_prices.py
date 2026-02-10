"""
Live price poller:
- Yahoo Finance + CCXT
- Dynamic symbol auto-discovery (ACTIVE/WATCH)
- Staleness detection + alerts
- Outlier price detection
- Shadow-mode realized return capture (for calibration)

Writes to SQLite prices table.
"""

import os
import time
import json
import random
import logging
import statistics
from typing import Dict, Any, Tuple

from dev_core.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
    put_event,
)

from dev_core.live_prices.yfinance_live import fetch_latest_ohlcv_yf
from dev_core.live_prices.ccxt_live import fetch_last_prices_ccxt, fetch_latest_ohlcv_ccxt
from dev_core.live_prices.provider import get_price_provider, get_price_provider_by_name
from dev_core.universe import get_active_symbols
from dev_core.symbol_blacklist import is_blacklisted
from dev_core.portfolio_risk_gate import apply_portfolio_risk_gate
from dev_core.alerts import emit_alert

# ------            -- ------------------------------------------------------
# Runtime config
# ------            -- ------------------------------------------------------

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "30"))
PRICE_STALE_AFTER_S = int(os.environ.get("PRICE_STALE_AFTER_S", "120"))

OUTLIER_LOOKBACK = int(os.environ.get("PRICE_OUTLIER_LOOKBACK", "30"))
OUTLIER_Z = float(os.environ.get("PRICE_OUTLIER_Z", "3.5"))

JOB_NAME = "poll_prices"
OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
)
PID = os.getpid()

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [poll_prices] %(message)s",
)

FAIL_BASE_S = float(os.environ.get("POLL_FAIL_BASE_S", "2.0"))
FAIL_MAX_S = float(os.environ.get("POLL_FAIL_MAX_S", "60.0"))

HEARTBEAT_EVERY_S = float(os.environ.get("HEARTBEAT_EVERY_S", "15.0"))
LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))

# ------            -- ------------------------------------------------------
# Helpers
# ------            -- ------------------------------------------------------

def _put_provider_health(con, ts_ms: int, provider: str, ok: int, latency_ms: int, n_symbols: int, error: str = None) -> None:
    con.execute(
        """
        INSERT INTO price_provider_health(ts_ms, provider, ok, latency_ms, n_symbols, error)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(provider, ts_ms) DO UPDATE SET
          ok=excluded.ok,
          latency_ms=excluded.latency_ms,
          n_symbols=excluded.n_symbols,
          error=excluded.error
        """,
        (
            int(ts_ms),
            str(provider),
            int(ok),
            (int(latency_ms) if latency_ms is not None else None),
            int(n_symbols),
            (str(error) if error else None),
        ),
    )


def _put_quotes_batch(con, rows):
    """
    rows: [(ts_ms, symbol, last, bid, ask, spread, volume, source), ...]
    (final / ensemble)
    """
    con.executemany(
        """
        INSERT INTO price_quotes(ts_ms, symbol, last, bid, ask, spread, volume, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, ts_ms) DO UPDATE SET
          last=excluded.last,
          bid=excluded.bid,
          ask=excluded.ask,
          spread=excluded.spread,
          volume=excluded.volume,
          source=excluded.source
        """,
        rows,
    )


def _put_quotes_raw_batch(con, rows):
    """
    rows: [(ts_ms, symbol, provider, last, bid, ask, spread, volume), ...]
    (raw per-provider)
    """
    con.executemany(
        """
        INSERT INTO price_quotes_raw(ts_ms, symbol, provider, last, bid, ask, spread, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, provider, ts_ms) DO UPDATE SET
          last=excluded.last,
          bid=excluded.bid,
          ask=excluded.ask,
          spread=excluded.spread,
          volume=excluded.volume
        """,
        rows,
    )


def _put_ingest_slippage_batch(con, rows):
    """
    rows: [(ts_ms, symbol, provider, last, bid, ask, mid, spread, px_minus_mid, abs_px_minus_mid), ...]
    """
    con.executemany(
        """
        INSERT INTO ingest_slippage(
          ts_ms, symbol, provider,
          last, bid, ask, mid, spread,
          px_minus_mid, abs_px_minus_mid
        )
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(symbol, provider, ts_ms) DO UPDATE SET
          last=excluded.last,
          bid=excluded.bid,
          ask=excluded.ask,
          mid=excluded.mid,
          spread=excluded.spread,
          px_minus_mid=excluded.px_minus_mid,
          abs_px_minus_mid=excluded.abs_px_minus_mid
        """,
        rows,
    )


def _compute_provider_weights(con, provider_names, now_ts_ms: int):
    """
    Weights by recent OK-rate and low ingest slippage.
    Returns: {provider: weight}
    """
    window_ms = int(float(os.environ.get("ENSEMBLE_WEIGHT_WINDOW_S", "300")) * 1000.0)
    cutoff = int(now_ts_ms - window_ms)

    names = [str(p) for p in (provider_names or []) if p]
    if not names:
        return {}

    # default equal weights
    w = {p: 1.0 for p in names}

    try:
        q = ",".join(["?"] * len(names))

        # ok-rate
        rows = con.execute(
            f"""
            SELECT provider,
                   AVG(CASE WHEN ok=1 THEN 1.0 ELSE 0.0 END) AS ok_rate,
                   AVG(COALESCE(latency_ms,0)) AS avg_lat
            FROM price_provider_health
            WHERE ts_ms >= ? AND provider IN ({q})
            GROUP BY provider
            """,
            (int(cutoff), *names),
        ).fetchall() or []

        ok_rate = {str(p): float(r) for (p, r, _lat) in rows if p is not None and r is not None}

        # avg ingest abs slippage (lower is better)
        rows2 = con.execute(
            f"""
            SELECT provider, AVG(abs_px_minus_mid) AS avg_abs
            FROM ingest_slippage
            WHERE ts_ms >= ? AND provider IN ({q})
            GROUP BY provider
            """,
            (int(cutoff), *names),
        ).fetchall() or []

        avg_abs = {str(p): float(a) for (p, a) in rows2 if p is not None and a is not None}

        slip_scale = float(os.environ.get("ENSEMBLE_SLIP_SCALE", "1.0"))
        min_ok = float(os.environ.get("ENSEMBLE_MIN_OK_RATE", "0.20"))

        for p in names:
            r = ok_rate.get(p, 1.0)
            if r < min_ok:
                w[p] = 0.05
                continue
            a = avg_abs.get(p, 0.0)
            # downweight if abs deviation from mid is higher
            w[p] = max(0.05, float(r) / (1.0 + slip_scale * float(a)))

        # normalize
        s = sum(w.values()) or 1.0
        for p in list(w.keys()):
            w[p] = float(w[p]) / float(s)

        return w

    except Exception:
        # equal weights fallback
        s = float(len(names))
        return {p: 1.0 / s for p in names}

def _sleep_with_jitter(seconds: float) -> None:
    if seconds <= 0:
        return
    j = seconds * 0.2
    time.sleep(max(0.05, seconds + random.uniform(-j, j)))


def _load_symbol_providers() -> Tuple[Dict[str, str], Dict[str, str]]:
    owns = False
    if con is None:
        con = connect()
        owns = True
    try:
        rows = con.execute(
            """
            SELECT symbol, meta_json
            FROM symbols
            WHERE status IN ('ACTIVE','WATCH')

            """
        ).fetchall()
    finally:
        con.close()

    yf_map: Dict[str, str] = {}
    ccxt_map: Dict[str, str] = {}

    for sym, meta_json in rows:
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except Exception:
            meta = {}

        provider = (meta.get("price_provider") or "").lower()
        if provider == "yfinance":
            yf_map[sym] = meta.get("yf_ticker", sym)
        elif provider == "ccxt":
            ccxt_map[sym] = meta.get("ccxt_market")

    # ------            -- ------------------------------------------------------
    # Ensure global stress proxy (VIX) is always present
    # ------            -- ------------------------------------------------------
    if "VIX" not in yf_map:
        yf_map["VIX"] = "^VIX"

    # ------            -- ------------------------------------------------------
    # Ensure Tier-1 macro/credit/flows proxies are present (YF)
    # These are used by compute_factor_features.py (factor universe)
    # ------            -- ------------------------------------------------------
    if os.environ.get("FORCE_FACTOR_PROXY_TICKERS", "1") == "1":
        # Rates (Yahoo caret indices)
        yf_map.setdefault("TNX", "^TNX")  # 10Y yield index (Yahoo convention)
        yf_map.setdefault("FVX", "^FVX")  # 5Y yield index (proxy for short rates)

        # Credit proxies (ETF prices)
        yf_map.setdefault("HYG", "HYG")
        yf_map.setdefault("LQD", "LQD")

        # Risk appetite proxy (ETF ratio)
        yf_map.setdefault("SPY", "SPY")
        yf_map.setdefault("AGG", "AGG")

    return yf_map, ccxt_map


def _detect_outlier(prices: list, latest: float) -> bool:
    if len(prices) < OUTLIER_LOOKBACK:
        return False
    try:
        med = statistics.median(prices)
        mad = statistics.median([abs(p - med) for p in prices]) or 1e-9
        z = abs(latest - med) / mad
        return z >= OUTLIER_Z
    except Exception:
        return False
def _put_prices_batch(con, rows):
    """
    rows: [(ts_ms, symbol, price), ...]
    """
    con.executemany(
        """
        INSERT INTO prices(ts_ms, symbol, price)
        VALUES (?, ?, ?)
        ON CONFLICT(symbol, ts_ms) DO UPDATE SET
          price=excluded.price
        """,
        rows,
    )

    now_ms = int(time.time() * 1000)
    for ts_ms, sym, _ in rows:
        

        con.execute(
            """
            UPDATE symbols SET
              updated_ts_ms=?,
              meta_json=json_set(
                COALESCE(meta_json,'{}'),
                '$.price_status.last_seen_ts_ms', ?
              )
            WHERE symbol=?
            """,
            (now_ms, int(ts_ms), sym),
        )


def _put_bar(tf_s: int, ts_ms: int, symbol: str, o: float, h: float, l: float, c: float, v) -> None:
    con = connect()
    try:
        

        con.execute(
            """
            INSERT OR REPLACE INTO price_bars(tf_s, ts_ms, symbol, o, h, l, c, v)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (int(tf_s), int(ts_ms), str(symbol), float(o), float(h), float(l), float(c), (float(v) if v is not None else None)),
        )
        con.commit()
    finally:
        try:
            con.close()
        except Exception:
            pass

def _mark_stale(now_ts_ms: int) -> None:
    cutoff = now_ts_ms - PRICE_STALE_AFTER_S * 1000
    owns = False
    if con is None:
        con = connect()
        owns = True
    try:
        rows = con.execute(
            "SELECT symbol, meta_json FROM symbols WHERE status IN ('ACTIVE','WATCH')"
        ).fetchall()

        for sym, meta_json in rows:
            try:
                meta = json.loads(meta_json) if meta_json else {}
            except Exception:
                meta = {}

            last_seen = meta.get("price_status", {}).get("last_seen_ts_ms")
            if last_seen and int(last_seen) < cutoff:
                meta.setdefault("price_status", {})
                meta["price_status"]["stale"] = True

                emit_alert(
                    event_title=f"Price stale: {sym}",
                    symbol=sym,
                    horizon_s=0,
                    expected_z=0.0,
                    confidence=1.0,
                    explain={
                        "last_seen_ts_ms": last_seen,
                        "stale_for_s": int((now_ts_ms - last_seen) / 1000),
                        "type": "price_stale",
                    },
                )

                pass

        con.execute(
                    "UPDATE symbols SET meta_json=?, updated_ts_ms=? WHERE symbol=?",
                    (json.dumps(meta, separators=(",", ":")), now_ts_ms, sym),
                )

        con.commit()
    finally:
        con.close()

# ------            -- ------------------------------------------------------
# Main loop
# ------            -- ------------------------------------------------------

def main():
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S):
        raise SystemExit(2)

    # Provider failover chain: "polygon,yfinance" (default falls back to LIVE_PRICE_PROVIDER)
    chain = [p.strip().lower() for p in os.environ.get("LIVE_PRICE_PROVIDER_CHAIN", "").split(",") if p.strip()]
    if not chain:
        chain = [os.environ.get("LIVE_PRICE_PROVIDER", "yfinance").lower().strip()]

    providers = []
    for name in chain:
        try:
            providers.append((name, get_price_provider_by_name(name)))
        except Exception:
            continue

    if not providers:
        providers = [("yfinance", get_price_provider_by_name("yfinance"))]

    yf_provider = get_price_provider()
    fail_s = 0.0
    last_hb_s = 0.0

    try:
        while True:
            now_s = time.time()
            now_ts_ms = int(now_s * 1000)

            if now_s - last_hb_s >= HEARTBEAT_EVERY_S:
                touch_job_lock(JOB_NAME, OWNER, PID)
                put_job_heartbeat(
                    JOB_NAME,
                    OWNER,
                    PID,
                    extra_json=json.dumps(
                        {
                            "poll_seconds": POLL_SECONDS,
                            "fail_backoff_s": fail_s,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                )
                last_hb_s = now_s

            yf_map, ccxt_map = _load_symbol_providers()
            merged = {}

            if yf_map:
                for pname, prov in providers:
                    t0 = time.time()
                    err = None
                    got = {}
                    ok = 0
                    try:
                        got = prov.fetch_last_prices(yf_map) or {}
                        ok = 1 if got else 0
                    except Exception as e:
                        err = repr(e)
                        got = {}
                        ok = 0

                    latency_ms = int((time.time() - t0) * 1000)
                    try:
                        conh = connect()
                        try:
                            _put_provider_health(conh, now_ts_ms, pname, ok, latency_ms, len(yf_map), err)
                            conh.commit()
                        finally:
                            conh.close()
                    except Exception:
                        pass

                    if got:
                        # ensure provider source is tagged
                        for sym, p in got.items():
                            if isinstance(p, dict) and (not p.get("source")):
                                p["source"] = pname
                        merged.update(got)
                        break

            provider_names = [n for (n, _p) in providers]

            got_by_provider = {}
            raw_quote_rows = []  # (ts_ms, sym, provider, last, bid, ask, spread, vol)
            slip_rows = []       # (ts_ms, sym, provider, last, bid, ask, mid, spread, pxm, abs_pxm)

            if yf_map:
                for pname, prov in providers:
                    t0 = time.time()
                    ok = 0
                    err = None
                    got = {}
                    try:
                        got = prov.fetch_last_prices(yf_map) or {}
                        ok = 1 if got else 0
                    except Exception as e:
                        err = repr(e)
                        got = {}
                        ok = 0

                    latency_ms = int((time.time() - t0) * 1000)

                    try:
                        conh = connect()
                        try:
                            _put_provider_health(conh, now_ts_ms, pname, ok, latency_ms, len(yf_map), err)
                            conh.commit()
                        finally:
                            conh.close()
                    except Exception:
                        pass

                    if got:
                        # normalize + collect raw rows
                        for sym, p in (got or {}).items():
                            if not isinstance(p, dict):
                                continue
                            if not p.get("source"):
                                p["source"] = pname

                            ts_ms = int(p.get("ts_ms") or now_ts_ms)
                            last = p.get("price")
                            bid = p.get("bid")
                            ask = p.get("ask")
                            spr = p.get("spread")
                            vol = p.get("volume")

                            if last is not None:
                                raw_quote_rows.append(
                                    (
                                        int(ts_ms),
                                        str(sym),
                                        str(pname),
                                        float(last),
                                        (float(bid) if bid is not None else None),
                                        (float(ask) if ask is not None else None),
                                        (float(spr) if spr is not None else (float(ask) - float(bid) if (bid is not None and ask is not None) else None)),
                                        (float(vol) if vol is not None else None),
                                    )
                                )

                            if (last is not None) and (bid is not None) and (ask is not None):
                                try:
                                    mid = (float(bid) + float(ask)) / 2.0
                                    pxm = float(last) - float(mid)
                                    spread = float(spr) if spr is not None else (float(ask) - float(bid))
                                    slip_rows.append(
                                        (
                                            int(ts_ms),
                                            str(sym),
                                            str(pname),
                                            float(last),
                                            float(bid),
                                            float(ask),
                                            float(mid),
                                            float(spread),
                                            float(pxm),
                                            float(abs(pxm)),
                                        )
                                    )
                                except Exception:
                                    pass

                        got_by_provider[pname] = got

            # CCXT (kept separate; still merged as additional symbols)
            if ccxt_map:
                merged.update(fetch_last_prices_ccxt("binance", ccxt_map) or {})

            # Provider-weighted ensemble for yf_map symbols
            if got_by_provider:
                conw = connect()
                try:
                    weights = _compute_provider_weights(conw, list(got_by_provider.keys()), now_ts_ms)

                    for sym in yf_map.keys():
                        parts = []
                        best_quote = None
                        best_w = -1.0

                        for pname, got in got_by_provider.items():
                            p = (got or {}).get(sym)
                            if not isinstance(p, dict):
                                continue
                            px = p.get("price")
                            if px is None:
                                continue

                            w = float(weights.get(pname, 0.0))
                            if w <= 0.0:
                                continue

                            ts_ms = int(p.get("ts_ms") or now_ts_ms)
                            bid = p.get("bid")
                            ask = p.get("ask")
                            spr = p.get("spread")
                            vol = p.get("volume")

                            parts.append((w, float(px), ts_ms))

                            # choose best quote source (highest weight with bid/ask)
                            if (bid is not None and ask is not None) and w > best_w:
                                best_w = w
                                best_quote = {
                                    "bid": float(bid),
                                    "ask": float(ask),
                                    "spread": (float(spr) if spr is not None else (float(ask) - float(bid))),
                                    "volume": (float(vol) if vol is not None else None),
                                    "source": str(pname),
                                    "ts_ms": int(ts_ms),
                                }

                        if not parts:
                            continue

                        px_ens = sum(w * px for (w, px, _t) in parts)
                        ts_ens = max(t for (_w, _px, t) in parts)

                        merged[str(sym)] = {
                            "ts_ms": int(ts_ens),
                            "price": float(px_ens),
                            "bid": (best_quote["bid"] if best_quote else None),
                            "ask": (best_quote["ask"] if best_quote else None),
                            "spread": (best_quote["spread"] if best_quote else None),
                            "volume": (best_quote["volume"] if best_quote else None),
                            "source": "ensemble",
                        }

                    # persist raw quotes + ingest slippage proxy
                    if raw_quote_rows:
                        _put_quotes_raw_batch(conw, raw_quote_rows)
                    if slip_rows:
                        _put_ingest_slippage_batch(conw, slip_rows)

                    conw.commit()
                finally:
                    try:
                        conw.close()
                    except Exception:
                        pass

            if merged:
                conw = connect()
                try:
                    price_rows = [(int(p["ts_ms"]), sym, float(p["price"])) for sym, p in merged.items()]
                    _put_prices_batch(conw, price_rows)

                    quote_rows = []
                    for sym, p in merged.items():
                        bid = p.get("bid")
                        ask = p.get("ask")
                        spread = p.get("spread")
                        vol = p.get("volume")
                        src = p.get("source")
                        # store quotes only if any quote field exists
                        if (bid is not None) or (ask is not None) or (spread is not None) or (vol is not None) or (src is not None):
                            quote_rows.append(
                                (
                                    int(p["ts_ms"]),
                                    sym,
                                    float(p["price"]),
                                    (float(bid) if bid is not None else None),
                                    (float(ask) if ask is not None else None),
                                    (float(spread) if spread is not None else None),
                                    (float(vol) if vol is not None else None),
                                    (str(src) if src is not None else None),
                                )
                            )

                    if quote_rows:
                        _put_quotes_batch(conw, quote_rows)

                    conw.commit()
                finally:
                    try:
                        conw.close()
                    except Exception:
                        pass

                logging.info("prices=%s", {k: v["price"] for k, v in merged.items()})
                fail_s = 0.0

            else:
                fail_s = min(FAIL_MAX_S, fail_s * 2.0 if fail_s else FAIL_BASE_S)

            _mark_stale(now_ts_ms)
            _sleep_with_jitter(fail_s or POLL_SECONDS)

    finally:
        release_job_lock(JOB_NAME, OWNER, PID)


if __name__ == "__main__":
    main()
