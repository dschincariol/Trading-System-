"""
IBKR Real-Time Market Data Daemon
reqMktData stream -> SQLite

Writes:
  price_quotes_raw
  price_quotes
  prices

Env:
  IBKR_HOST=127.0.0.1
  IBKR_PORT=7497
  IBKR_CLIENT_ID=9001
  IBKR_DATA_TYPE=1  (1=LIVE, 2=FROZEN, 3=DELAYED, 4=DELAYED_FROZEN)

  STREAM_PRICES_FLUSH_MS=250
"""

import os
import sys
import time
import threading
import asyncio
import logging
from typing import Dict
from dotenv import load_dotenv
load_dotenv()

if os.environ.get("ENGINE_SUPERVISED") != "1":
    print("stream_prices_ibkr must be launched by supervisor")
    sys.exit(1)
    
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

from engine.dev_core.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
)

JOB_NAME = "stream_prices_ibkr"
OWNER = os.environ.get("JOB_OWNER", "system")
PID = os.getpid()
LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))

IBKR_HOST = os.environ.get("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.environ.get("IBKR_PORT", "7497"))
IBKR_CLIENT_ID = int(os.environ.get("IBKR_CLIENT_ID", "9001"))
IBKR_DATA_TYPE = int(os.environ.get("IBKR_DATA_TYPE", "1"))

FLUSH_MS = int(os.environ.get("STREAM_PRICES_FLUSH_MS", "250"))

def now_ms():
    return int(time.time() * 1000)

def load_symbols():
    con = connect(readonly=True)
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

    out = {}
    for sym, meta_json in rows:
        out[str(sym)] = str(sym)
    return out


class IBKRWrapper(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.lock = threading.Lock()
        self.req_id_to_symbol: Dict[int, str] = {}
        self.data: Dict[str, Dict] = {}

    def tickPrice(self, reqId, tickType, price, attrib):
        sym = self.req_id_to_symbol.get(reqId)
        if not sym:
            return
        with self.lock:
            rec = self.data.get(sym, {})
            if tickType == 1:
                rec["bid"] = price
            elif tickType == 2:
                rec["ask"] = price
            elif tickType == 4:
                rec["last"] = price
            rec["ts_ms"] = now_ms()
            self.data[sym] = rec

    def tickSize(self, reqId, tickType, size):
        sym = self.req_id_to_symbol.get(reqId)
        if not sym:
            return
        with self.lock:
            rec = self.data.get(sym, {})
            if tickType == 5:
                rec["volume"] = size
            rec["ts_ms"] = now_ms()
            self.data[sym] = rec


def create_contract(symbol: str):
    c = Contract()
    c.symbol = symbol
    c.secType = "STK"
    c.exchange = "SMART"
    c.currency = "USD"
    return c


def flush_to_db(wrapper: IBKRWrapper):
    snap = {}
    with wrapper.lock:
        snap = dict(wrapper.data)

    if not snap:
        return

    con = connect(readonly=False)
    try:
        con.execute("BEGIN IMMEDIATE;")
        for sym, rec in snap.items():
            ts = int(rec.get("ts_ms") or now_ms())
            last = rec.get("last")
            bid = rec.get("bid")
            ask = rec.get("ask")
            volume = rec.get("volume")
            spread = (ask - bid) if (ask and bid) else None

            con.execute(
                """
                INSERT OR REPLACE INTO price_quotes_raw
                (ts_ms, symbol, provider, last, bid, ask, spread, volume)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (ts, sym, "ibkr", last, bid, ask, spread, volume),
            )

            con.execute(
                """
                INSERT OR REPLACE INTO price_quotes
                (ts_ms, symbol, last, bid, ask, spread, volume, source)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (ts, sym, last, bid, ask, spread, volume, "ibkr"),
            )

            if last is not None:
                con.execute(
                    """
                    INSERT OR REPLACE INTO prices
                    (ts_ms, symbol, px, source)
                    VALUES (?,?,?,?)
                    """,
                    (ts, sym, last, "ibkr"),
                )

        con.execute("COMMIT;")
    except Exception:
        con.execute("ROLLBACK;")
    finally:
        con.close()


def main():
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S):
        raise SystemExit(2)

    wrapper = IBKRWrapper()
    wrapper.connect(IBKR_HOST, IBKR_PORT, IBKR_CLIENT_ID)
    wrapper.reqMarketDataType(IBKR_DATA_TYPE)

    thread = threading.Thread(target=wrapper.run, daemon=True)
    thread.start()

    symbols = load_symbols()
    req_id = 1
    for sym in symbols:
        contract = create_contract(sym)
        wrapper.reqMktData(req_id, contract, "", False, False, [])
        wrapper.req_id_to_symbol[req_id] = sym
        req_id += 1
        time.sleep(0.05)

    last_hb = 0

    try:
        while True:
            flush_to_db(wrapper)

            if time.time() - last_hb > 2:
                touch_job_lock(JOB_NAME, OWNER, PID)
                put_job_heartbeat(JOB_NAME, OWNER, PID)
                last_hb = time.time()

            time.sleep(FLUSH_MS / 1000.0)

    finally:
        wrapper.disconnect()
        release_job_lock(JOB_NAME, OWNER, PID)


if __name__ == "__main__":
    main()
