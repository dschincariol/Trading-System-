import os
import time
from dev_core.storage import connect


def _provider_health_key(name: str) -> str:
    return f"price_provider_health::{name}"


def _record_provider_failure(name: str):
    con = connect()
    try:
        ok = 0 if had_error else 1

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
        ok = 0 if had_error else 1

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

def get_price_provider():
    provider = os.environ.get("LIVE_PRICE_PROVIDER", "yfinance").lower()

    # --------------------------------------------------
    # Preferred provider
    # --------------------------------------------------
    if provider == "polygon":
        try:
            from dev_core.live_prices.polygon_live import PolygonPriceProvider
            p = PolygonPriceProvider()
            _record_provider_success("polygon")
            return p
        except Exception:
            _record_provider_failure("polygon")

    if provider == "ccxt":
        try:
            from dev_core.live_prices.ccxt_live import CCXTPriceProvider
            p = CCXTPriceProvider()
            _record_provider_success("ccxt")
            return p
        except Exception:
            _record_provider_failure("ccxt")

    if provider == "yfinance":
        try:
            from dev_core.live_prices.yfinance_live import YFinancePriceProvider
            p = YFinancePriceProvider()
            _record_provider_success("yfinance")
            return p
        except Exception:
            _record_provider_failure("yfinance")

    # --------------------------------------------------
    # HARD FAILOVER CHAIN (deterministic)
    # --------------------------------------------------
    try:
        from dev_core.live_prices.polygon_live import PolygonPriceProvider
        p = PolygonPriceProvider()
        _record_provider_success("polygon")
        return p
    except Exception:
        _record_provider_failure("polygon")

    try:
        from dev_core.live_prices.yfinance_live import YFinancePriceProvider
        p = YFinancePriceProvider()
        _record_provider_success("yfinance")
        return p
    except Exception:
        _record_provider_failure("yfinance")

    try:
        from dev_core.live_prices.ccxt_live import CCXTPriceProvider
        p = CCXTPriceProvider()
        _record_provider_success("ccxt")
        return p
    except Exception:
        _record_provider_failure("ccxt")

    raise RuntimeError("No live price provider available")
