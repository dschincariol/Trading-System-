# FILE: dev_core/universe_discovery.py
# REPLACE ENTIRE FILE WITH THIS (copy/paste)

# dev_core/universe_discovery.py
"""
Universe Discovery Engine.

Goal:
- Maintain a large candidate universe (symbols table)
- Dynamically filter & rank assets by tradability + opportunity
- Correlation-cluster pruning
- Regime suitability
- Persist selection decisions with reasons (universe_audit)

Deterministic:
- All scoring uses bounded lookbacks and deterministic ordering.
- Exclusion is explicit + audited.

Outputs:
- Updates symbols.status to ACTIVE or WATCH (never overrides DISABLED)
- Writes universe_audit rows per decision cycle.

Time-bounded correlation pruning:
- UNIVERSE_CORR_MAX_PAIRS caps correlation evaluations
- UNIVERSE_CORR_TIME_BUDGET_MS caps wall-clock time spent in correlation pruning
- Graceful early exit: if budgets exceed, stops further pruning and proceeds with already-selected set
- Audit visibility: adds corr_pairs_evaluated, corr_time_exceeded, corr_max_pairs, corr_time_budget_ms to result
"""

import json
import math
import os
import time
from typing import Any, Dict, List, Tuple, Optional

from dev_core.storage import connect, init_db
from dev_core.regime import get_current_regime


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ensure_schema(con) -> None:
    con.executescript(
        """
CREATE TABLE IF NOT EXISTS universe_audit (
  ts_ms INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  status_before TEXT,
  status_after TEXT,
  include INTEGER NOT NULL,
  score REAL,
  reasons_json TEXT,
  features_json TEXT,
  PRIMARY KEY (ts_ms, symbol)
);

CREATE INDEX IF NOT EXISTS idx_universe_audit_ts
  ON universe_audit(ts_ms);

CREATE INDEX IF NOT EXISTS idx_universe_audit_symbol_ts
  ON universe_audit(symbol, ts_ms);
"""
    )


def _safe_f(x, d=0.0) -> float:
    try:
        v = float(x)
        if math.isfinite(v):
            return v
    except Exception:
        pass
    return float(d)


def _latest_quote(con, symbol: str) -> Optional[Tuple[int, float, float, float, float]]:
    """
    Returns (ts_ms, last, bid, ask, volume) or None
    """
    try:
        r = con.execute(
            """
            SELECT ts_ms, last, bid, ask, volume
            FROM price_quotes
            WHERE symbol=?
            ORDER BY ts_ms DESC
            LIMIT 1
            """,
            (str(symbol),),
        ).fetchone()
        if not r:
            return None
        return int(r[0] or 0), _safe_f(r[1]), _safe_f(r[2]), _safe_f(r[3]), _safe_f(r[4])
    except Exception:
        return None


def _returns_std_from_bars(con, symbol: str, lookback: int) -> Optional[float]:
    """
    Uses price_bars close-to-close log returns stdev over bounded lookback.
    """
    try:
        rows = con.execute(
            """
            SELECT c
            FROM price_bars
            WHERE symbol=?
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (str(symbol), int(lookback)),
        ).fetchall()
        if not rows or len(rows) < 5:
            return None
        closes = [float(r[0]) for r in rows if r and r[0] is not None]
        if len(closes) < 5:
            return None
        closes = list(reversed(closes))
        rets = []
        for i in range(1, len(closes)):
            a = float(closes[i - 1])
            b = float(closes[i])
            if a > 0 and b > 0:
                rets.append(math.log(b / a))
        if len(rets) < 4:
            return None
        mu = sum(rets) / float(len(rets))
        var = sum((x - mu) ** 2 for x in rets) / float(max(1, len(rets) - 1))
        return math.sqrt(max(0.0, var))
    except Exception:
        return None


def _corr(con, a: str, b: str, lookback: int) -> Optional[float]:
    """
    Pearson correlation of log returns over lookback bars.
    """
    try:
        ra = con.execute(
            """
            SELECT ts_ms, c
            FROM price_bars
            WHERE symbol=?
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (str(a), int(lookback)),
        ).fetchall()
        rb = con.execute(
            """
            SELECT ts_ms, c
            FROM price_bars
            WHERE symbol=?
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (str(b), int(lookback)),
        ).fetchall()
        if not ra or not rb:
            return None

        ma = {int(ts): _safe_f(c, None) for (ts, c) in ra if ts is not None and c is not None}
        mb = {int(ts): _safe_f(c, None) for (ts, c) in rb if ts is not None and c is not None}
        ts_common = sorted(set(ma.keys()) & set(mb.keys()))
        if len(ts_common) < 8:
            return None

        # build aligned returns
        ca = [ma[t] for t in ts_common]
        cb = [mb[t] for t in ts_common]
        retsa = []
        retsb = []
        for i in range(1, len(ts_common)):
            pa0, pa1 = ca[i - 1], ca[i]
            pb0, pb1 = cb[i - 1], cb[i]
            if pa0 > 0 and pa1 > 0 and pb0 > 0 and pb1 > 0:
                retsa.append(math.log(pa1 / pa0))
                retsb.append(math.log(pb1 / pb0))
        n = min(len(retsa), len(retsb))
        if n < 6:
            return None
        retsa = retsa[-n:]
        retsb = retsb[-n:]
        ma_ = sum(retsa) / n
        mb_ = sum(retsb) / n
        cov = sum((retsa[i] - ma_) * (retsb[i] - mb_) for i in range(n))
        va = sum((retsa[i] - ma_) ** 2 for i in range(n))
        vb = sum((retsb[i] - mb_) ** 2 for i in range(n))
        den = math.sqrt(max(1e-12, va * vb))
        return float(cov / den)
    except Exception:
        return None


def discover_universe_once(
    con=None,
    ts_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Runs a single discovery cycle.

    Env controls:
      UNIVERSE_CANDIDATE_MAX=500
      UNIVERSE_ACTIVE_TARGET=80
      UNIVERSE_WATCH_TARGET=140

      UNIVERSE_QUOTE_MAX_AGE_S=120
      UNIVERSE_MIN_DOLLAR_VOL=1000000
      UNIVERSE_SPREAD_BPS_MAX=40
      UNIVERSE_VOL_MAX=0.08

      UNIVERSE_CORR_LOOKBACK_BARS=96
      UNIVERSE_CORR_TH=0.85
      UNIVERSE_CLUSTER_MAX=200

    Time budget controls:
      UNIVERSE_CORR_MAX_PAIRS=5000
      UNIVERSE_CORR_TIME_BUDGET_MS=1200
    """
    owns = False
    if con is None:
        con = connect()
        owns = True
    try:
        init_db()
        _ensure_schema(con)

        ts_ms = int(ts_ms or _now_ms())
        now_s = ts_ms / 1000.0

        cand_max = int(os.environ.get("UNIVERSE_CANDIDATE_MAX", "500"))
        active_target = int(os.environ.get("UNIVERSE_ACTIVE_TARGET", "80"))
        watch_target = int(os.environ.get("UNIVERSE_WATCH_TARGET", "140"))

        quote_max_age_s = float(os.environ.get("UNIVERSE_QUOTE_MAX_AGE_S", "120"))
        min_dollar_vol = float(os.environ.get("UNIVERSE_MIN_DOLLAR_VOL", "1000000"))
        spread_bps_max = float(os.environ.get("UNIVERSE_SPREAD_BPS_MAX", "40"))
        vol_max = float(os.environ.get("UNIVERSE_VOL_MAX", "0.08"))

        corr_lookback = int(os.environ.get("UNIVERSE_CORR_LOOKBACK_BARS", "96"))
        corr_th = float(os.environ.get("UNIVERSE_CORR_TH", "0.85"))
        cluster_max = int(os.environ.get("UNIVERSE_CLUSTER_MAX", "200"))

        # NEW: time-bounded correlation pruning
        corr_max_pairs = int(os.environ.get("UNIVERSE_CORR_MAX_PAIRS", "5000"))
        corr_time_budget_ms = int(os.environ.get("UNIVERSE_CORR_TIME_BUDGET_MS", "1200"))

        regime = "MID"
        try:
            regime = str(get_current_regime("SPY") or "MID").upper()
        except Exception:
            regime = "MID"

        # Pull candidates by base score, exclude DISABLED
        rows = con.execute(
            """
            SELECT symbol, status, score, COALESCE(meta_json, '')
            FROM symbols
            WHERE status != 'DISABLED'
            ORDER BY score DESC, symbol ASC
            LIMIT ?
            """,
            (int(cand_max),),
        ).fetchall() or []

        scored: List[Dict[str, Any]] = []
        for sym, status, base_score, meta_json in rows:
            sym = str(sym)
            status = str(status or "WATCH")
            base = _safe_f(base_score, 0.0)

            reasons = []
            feats = {"base_score": base, "regime": regime}

            q = _latest_quote(con, sym)
            if not q:
                reasons.append("no_quote")
                scored.append(
                    {
                        "symbol": sym,
                        "status_before": status,
                        "include": False,
                        "score": None,
                        "reasons": reasons,
                        "features": feats,
                    }
                )
                continue

            q_ts, last, bid, ask, vol = q
            age_s = max(0.0, now_s - (q_ts / 1000.0))
            feats.update({"quote_ts_ms": q_ts, "last": last, "bid": bid, "ask": ask, "volume": vol, "quote_age_s": age_s})

            if age_s > quote_max_age_s:
                reasons.append("stale_quote")

            mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else last
            spread = max(0.0, ask - bid) if (bid > 0 and ask > 0) else 0.0
            spread_bps = (spread / mid) * 10000.0 if mid and mid > 0 else 1e9
            feats.update({"mid": mid, "spread": spread, "spread_bps": spread_bps})

            if spread_bps > spread_bps_max:
                reasons.append("spread_too_wide")

            dollar_vol = max(0.0, vol * last)
            feats.update({"dollar_vol": dollar_vol})
            if dollar_vol < min_dollar_vol:
                reasons.append("illiquid")

            vol_std = _returns_std_from_bars(con, sym, lookback=min(512, max(32, corr_lookback)))
            feats.update({"ret_std": vol_std})
            if vol_std is None:
                reasons.append("no_bars")
            else:
                # Regime suitability: in HIGH regime, reject very high vol
                if vol_std > vol_max and regime in ("HIGH", "CRISIS"):
                    reasons.append("too_volatile_for_regime")
                elif vol_std > vol_max * 1.5:
                    reasons.append("too_volatile")

            include = (len(reasons) == 0)

            # Score
            liq_score = math.log1p(max(0.0, dollar_vol))
            spread_pen = min(1.0, max(0.0, spread_bps / max(1e-9, spread_bps_max)))
            vol_pen = 0.0
            if vol_std is not None:
                # normalize around vol_max
                vol_pen = min(1.0, max(0.0, (vol_std / max(1e-9, vol_max)) - 1.0))
            regime_bonus = 0.0
            if regime in ("LOW",):
                regime_bonus = 0.10
            elif regime in ("HIGH", "CRISIS"):
                regime_bonus = -0.10

            opp = base + (0.08 * liq_score) - (0.80 * spread_pen) - (0.60 * vol_pen) + regime_bonus
            feats.update({"liq_score": liq_score, "spread_pen": spread_pen, "vol_pen": vol_pen, "regime_bonus": regime_bonus})
            scored.append({"symbol": sym, "status_before": status, "include": include, "score": float(opp), "reasons": reasons, "features": feats})

        # Preselect: keep only includes, sort by score desc
        pool = [x for x in scored if x.get("include") and x.get("score") is not None]
        pool.sort(key=lambda x: (-float(x.get("score") or 0.0), str(x.get("symbol"))))

        # Correlation pruning (bounded)
        selected = []
        pruned = []

        considered = pool[: max(active_target + watch_target, 1)]
        considered = considered[: max(cluster_max, len(considered))] if len(considered) > cluster_max else considered

        corr_pairs_evaluated = 0
        corr_time_exceeded = False
        corr_start_ms = _now_ms()

        for item in considered:
            sym = str(item["symbol"])
            ok = True

            # greedy compare against already selected (up to ACTIVE target + some buffer)
            for s2 in selected[: max(1, active_target)]:
                # pair cap
                if corr_pairs_evaluated >= int(corr_max_pairs):
                    corr_time_exceeded = True
                    break

                # time budget
                if (_now_ms() - corr_start_ms) > int(corr_time_budget_ms):
                    corr_time_exceeded = True
                    break

                corr_pairs_evaluated += 1

                c = _corr(con, sym, str(s2["symbol"]), lookback=corr_lookback)
                if c is None:
                    continue
                if abs(float(c)) >= corr_th:
                    ok = False
                    item["include"] = False
                    item["reasons"] = list(item.get("reasons") or []) + [f"corr_pruned:{s2['symbol']}:{c:.3f}"]
                    break

            # if budgets exceeded, stop further pruning (graceful early exit)
            if corr_time_exceeded:
                break

            if ok:
                selected.append(item)
            else:
                pruned.append(item)

        # If correlation pruning stopped early due to budget, include remaining items by score order
        # (deterministic: preserve ordering in `considered`)
        if corr_time_exceeded:
            # Continue selecting without corr checks until we have enough to cover targets.
            for item in considered[len(selected) + len(pruned) :]:
                if len(selected) >= (active_target + watch_target):
                    break
                if item.get("include") and (item.get("score") is not None):
                    selected.append(item)

        # Final ACTIVE/WATCH
        active = selected[:active_target]
        watch = selected[active_target : active_target + max(0, watch_target - active_target)]

        active_set = set([x["symbol"] for x in active])
        watch_set = set([x["symbol"] for x in watch])

        # Apply updates
        #  - set ACTIVE for active_set
        #  - set WATCH for watch_set (excluding active) but never override DISABLED
        if active_set:
            con.execute(
                f"UPDATE symbols SET status='ACTIVE', updated_ts_ms=? WHERE status != 'DISABLED' AND symbol IN ({','.join('?' for _ in sorted(active_set))})",
                (ts_ms, *sorted(active_set)),
            )

        watch_only = sorted([s for s in watch_set if s not in active_set])
        if watch_only:
            con.execute(
                f"UPDATE symbols SET status='WATCH', updated_ts_ms=? WHERE status != 'DISABLED' AND status != 'ACTIVE' AND symbol IN ({','.join('?' for _ in watch_only)})",
                (ts_ms, *watch_only),
            )

        # Audit: write rows for all candidates we scored (bounded)
        for item in scored:
            sym = str(item["symbol"])
            st_before = str(item.get("status_before") or "")
            include = 1 if bool(item.get("include")) else 0
            score = item.get("score", None)

            # compute status_after deterministically
            st_after = st_before
            if sym in active_set:
                st_after = "ACTIVE"
            elif sym in watch_set:
                st_after = "WATCH"

            # Attach corr budget metadata to features_json (auditable, does not affect selection)
            feats = dict(item.get("features") or {})
            feats.setdefault("corr_budget", {})
            try:
                feats["corr_budget"]["corr_pairs_evaluated"] = int(corr_pairs_evaluated)
                feats["corr_budget"]["corr_time_exceeded"] = bool(corr_time_exceeded)
                feats["corr_budget"]["corr_max_pairs"] = int(corr_max_pairs)
                feats["corr_budget"]["corr_time_budget_ms"] = int(corr_time_budget_ms)
                feats["corr_budget"]["corr_th"] = float(corr_th)
                feats["corr_budget"]["corr_lookback"] = int(corr_lookback)
            except Exception:
                pass

            con.execute(
                """
                INSERT OR REPLACE INTO universe_audit(ts_ms, symbol, status_before, status_after, include, score, reasons_json, features_json)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    ts_ms,
                    sym,
                    st_before,
                    st_after,
                    int(include),
                    None if score is None else float(score),
                    json.dumps(item.get("reasons") or []),
                    json.dumps(feats),
                ),
            )

        return {
            "ok": True,
            "ts_ms": int(ts_ms),
            "regime": regime,
            "n_candidates": int(len(rows)),
            "n_scored": int(len(scored)),
            "n_pool": int(len(pool)),
            "n_active": int(len(active_set)),
            "n_watch": int(len(watch_set)),
            "corr_pairs_evaluated": int(corr_pairs_evaluated),
            "corr_time_exceeded": bool(corr_time_exceeded),
            "corr_max_pairs": int(corr_max_pairs),
            "corr_time_budget_ms": int(corr_time_budget_ms),
        }
    finally:
        if owns:
            try:
                con.commit()
            except Exception:
                pass
            try:
                con.close()
            except Exception:
                pass
