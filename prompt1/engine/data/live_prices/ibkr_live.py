# CREATE NEW FILE: dev_core/live_prices/ibkr_live.py
import os
import time
from typing import Dict, Optional

from ib_insync import IB, Stock, Contract


class IBKRPriceProvider:
    """
    Snapshot-based IBKR market data provider.

    ticker_map usage (symbol -> value):
      - if value is digits: treated as conId
      - else: treated as a symbol for Stock(value, 'SMART', currency)

    For GLOBAL equities:
      - prefer storing IBKR conId in symbols.meta_json and pass conId strings in ticker_map
      - conId avoids exchange/currency ambiguity
    """

    def __init__(self):
        self.host = os.environ.get("IBKR_HOST", "127.0.0.1").strip() or "127.0.0.1"
        self.port = int(os.environ.get("IBKR_PORT", "7497"))
        self.client_id = int(os.environ.get("IBKR_CLIENT_ID", "77"))
        self.currency = os.environ.get("IBKR_CURRENCY", "USD").strip() or "USD"
        self.timeout_s = float(os.environ.get("IBKR_SNAPSHOT_TIMEOUT_S", "3.5"))
        self.sleep_after_req_s = float(os.environ.get("IBKR_SNAPSHOT_SETTLE_S", "0.35"))
        self._ib: Optional[IB] = None

    def _ensure_connected(self) -> IB:
        if self._ib is not None and self._ib.isConnected():
            return self._ib

        ib = IB()
        ib.connect(self.host, self.port, clientId=self.client_id, timeout=self.timeout_s)
        self._ib = ib
        return ib

    def _contract_from_value(self, val: str) -> Contract:
        v = str(val or "").strip()
        if v.isdigit():
            c = Contract()
            c.conId = int(v)
            return c
        sym = v
        return Stock(sym, "SMART", self.currency)

    def fetch_last_prices(self, ticker_map: Dict[str, str]) -> Dict[str, float]:
        """
        Returns {symbol: last_price_float} for successfully retrieved symbols.
        Missing/unavailable symbols are omitted.
        """
        if not ticker_map:
            return {}

        ib = self._ensure_connected()

        # Build contracts
        syms = []
        contracts = []
        for sym, val in (ticker_map or {}).items():
            try:
                c = self._contract_from_value(val)
                syms.append(str(sym))
                contracts.append(c)
            except Exception:
                continue

        if not contracts:
            return {}

        # Qualify contracts (best-effort)
        try:
            ib.qualifyContracts(*contracts)
        except Exception:
            pass

        # Request snapshot market data
        tickers = []
        for c in contracts:
            try:
                t = ib.reqMktData(c, "", snapshot=True, regulatorySnapshot=False)
                tickers.append(t)
            except Exception:
                tickers.append(None)

        # Let IB populate snapshot fields
        try:
            ib.sleep(self.sleep_after_req_s)
        except Exception:
            time.sleep(self.sleep_after_req_s)

        out: Dict[str, float] = {}
        for i, t in enumerate(tickers):
            sym = syms[i]
            if not t:
                continue

            px = None
            try:
                if t.last is not None and float(t.last) > 0:
                    px = float(t.last)
                elif t.close is not None and float(t.close) > 0:
                    px = float(t.close)
                elif t.marketPrice() is not None and float(t.marketPrice()) > 0:
                    px = float(t.marketPrice())
            except Exception:
                px = None

            if px is not None and px > 0:
                out[sym] = px

        # Cancel any lingering subscriptions (best-effort)
        for t in tickers:
            try:
                if t and t.contract:
                    ib.cancelMktData(t.contract)
            except Exception:
                pass

        return out
