# REPLACE ENTIRE FILE: dev_core/live_prices/provider.py
import os
import time
from engine.storage import connect
from engine.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot


def _provider_health_key(name: str) -> str:
    return f"price_provider_health::{name}"


def _record_provider_failure(name: str):
    con = connect()
    try:
        con.execute(
            """
            INSERT INTO risk_state(key, value, updated_ts_ms)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value=excluded.value,
              updated_ts_ms=excluded.updated_ts_ms
            """,
            (_provider_health_key(name), "fail", int(time.time() * 1000)),
        )
    finally:
        con.close()


def _record_provider_success(name: str):
    con = connect()
    try:
        con.execute(
            """
            INSERT INTO risk_state(key, value, updated_ts_ms)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value=excluded.value,
              updated_ts_ms=excluded.updated_ts_ms
            """,
            (_provider_health_key(name), "ok", int(time.time() * 1000)),
        )
    finally:
        con.close()


def get_price_provider_by_name(provider: str):
    provider = str(provider or "").strip().lower()

    if provider == "ibkr":
        from engine.live_prices.ibkr_live import IBKRPriceProvider
        return IBKRPriceProvider()

    if provider == "polygon":
        from engine.live_prices.polygon_live import PolygonPriceProvider
        return PolygonPriceProvider()

    if provider == "ccxt":
        from engine.live_prices.ccxt_live import CCXTPriceProvider
        return CCXTPriceProvider()

    if provider == "yfinance":
        from engine.live_prices.yfinance_live import YFinancePriceProvider
        return YFinancePriceProvider()

    raise RuntimeError(f"Unknown live price provider: {provider}")


def get_price_provider():
    provider = os.environ.get("LIVE_PRICE_PROVIDER", "yfinance").lower()

    # --------------------------------------------------
    # Preferred provider
    # --------------------------------------------------
    try:
        p = get_price_provider_by_name(provider)
        _record_provider_success(provider)
        return p
    except Exception:
        _record_provider_failure(provider)

    # --------------------------------------------------
    # Optional explicit failover chain (comma-separated)
    # Example: LIVE_PRICE_PROVIDER_CHAIN="ibkr,polygon,yfinance,ccxt"
    # --------------------------------------------------
    chain = os.environ.get("LIVE_PRICE_PROVIDER_CHAIN", "").strip()
    if chain:
        for name in [x.strip().lower() for x in chain.split(",") if x.strip()]:
            try:
                p = get_price_provider_by_name(name)
                _record_provider_success(name)
                return p
            except Exception:
                _record_provider_failure(name)

    # --------------------------------------------------
    # HARD FAILOVER CHAIN (deterministic, legacy behavior + IBKR first)
    # --------------------------------------------------
    for name in ("ibkr", "polygon", "yfinance", "ccxt"):
        try:
            p = get_price_provider_by_name(name)
            _record_provider_success(name)
            return p
        except Exception:
            _record_provider_failure(name)

    raise RuntimeError("No live price provider available")
