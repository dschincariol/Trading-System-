# dev_core/storage.py
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict

DB_PATH = Path(os.environ.get("DB_PATH", "dev.db")).expanduser().resolve()

# Production-stable WAL defaults + performance tuning (env-controlled)
#
# Tune via env:
#   SQLITE_CACHE_KB=-2000000         (~2GB page cache)
#   SQLITE_MMAP_BYTES=30000000000    (~30GB mmap)
#   SQLITE_WAL_AUTOCHECKPOINT=2000
#   SQLITE_JOURNAL_SIZE_LIMIT=268435456
#
# -----------------------------
# Connection pooling + safety
# -----------------------------
_TLS = threading.local()

# WAL checkpoint batching (env-controlled)
#   SQLITE_WAL_CHECKPOINT_EVERY_WRITES=250
#   SQLITE_WAL_CHECKPOINT_EVERY_S=30
#   SQLITE_WAL_CHECKPOINT_MODE=PASSIVE|RESTART|TRUNCATE
_WAL_CKPT_EVERY_WRITES = int(os.environ.get("SQLITE_WAL_CHECKPOINT_EVERY_WRITES", "250"))
_WAL_CKPT_EVERY_S = float(os.environ.get("SQLITE_WAL_CHECKPOINT_EVERY_S", "30"))
_WAL_CKPT_MODE = os.environ.get("SQLITE_WAL_CHECKPOINT_MODE", "PASSIVE").strip().upper()

# Corruption hardening (practical)
#   SQLITE_QUICK_CHECK_EVERY_S=600   (10 min; set 0 to disable)
#   SQLITE_INTEGRITY_CHECK_ON_START=0/1
_QUICK_CHECK_EVERY_S = float(os.environ.get("SQLITE_QUICK_CHECK_EVERY_S", "600"))
_INTEGRITY_CHECK_ON_START = os.environ.get("SQLITE_INTEGRITY_CHECK_ON_START", "0") == "1"

# Internal counters (per-process; good enough for single-process workers)
_LAST_CKPT_MS = 0
_WRITE_SINCE_CKPT = 0
_LAST_QUICK_CHECK_MS = 0

_SQLITE_PRAGMAS = [

    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA temp_store=MEMORY;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA foreign_keys=ON;",
    f"PRAGMA cache_size={int(os.environ.get('SQLITE_CACHE_KB', '-2000000'))};",
    f"PRAGMA mmap_size={int(os.environ.get('SQLITE_MMAP_BYTES', '30000000000'))};",
    f"PRAGMA wal_autocheckpoint={int(os.environ.get('SQLITE_WAL_AUTOCHECKPOINT', '2000'))};",
    f"PRAGMA journal_size_limit={int(os.environ.get('SQLITE_JOURNAL_SIZE_LIMIT', '268435456'))};",
]

def _apply_pragmas(con: sqlite3.Connection, readonly: bool) -> None:
    con.row_factory = sqlite3.Row

    for p in _SQLITE_PRAGMAS:
        try:
            con.execute(p)
        except Exception:
            pass

    # Ensure WAL actually active (defensive)
    try:
        jm = con.execute("PRAGMA journal_mode;").fetchone()
        if jm and str(jm[0]).upper() != "WAL":
            con.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass


    # Defense-in-depth: block writes on read connections
    if readonly:
        try:
            con.execute("PRAGMA query_only=ON;")
        except Exception:
            pass

    # Safer defaults that reduce "foot-guns"
    try:
        con.execute("PRAGMA trusted_schema=OFF;")
    except Exception:
        pass
    try:
        con.execute("PRAGMA recursive_triggers=ON;")
    except Exception:
        pass


def _new_connection(*, readonly: bool) -> sqlite3.Connection:
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # Use URI mode for readonly connections when possible
    if readonly:
        # If DB doesn't exist yet, readonly open will fail; fall back to normal open.
        uri = f"file:{str(DB_PATH)}?mode=ro"
        try:
            con = sqlite3.connect(
                uri,
                uri=True,
                timeout=30.0,
                isolation_level=None,
                check_same_thread=False,
            )
        except Exception:
            con = sqlite3.connect(
                str(DB_PATH),
                timeout=30.0,
                isolation_level=None,
                check_same_thread=False,
            )
    else:
        con = sqlite3.connect(
            str(DB_PATH),
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )

    _apply_pragmas(con, readonly=readonly)

    # Optional deep check at startup (expensive; off by default)
    if _INTEGRITY_CHECK_ON_START and not readonly:
        try:
            row = con.execute("PRAGMA integrity_check;").fetchone()
            if row and str(row[0]).lower() != "ok":
                raise RuntimeError("SQLite integrity_check failed")
        except Exception:
            raise

    return con

def connect(readonly: bool = False):
    """
    SQLite connection factory.

    readonly=True:
      - opens DB in read-only mode (where supported) to avoid accidental writes
      - applies query_only=ON defense-in-depth
      - reuses a per-thread pooled connection
    """
    key = "ro" if readonly else "rw"
    con = getattr(_TLS, key, None)

    if con is not None:
        try:
            con.execute("SELECT 1;").fetchone()
            return con
        except Exception:
            try:
                con.close()
            except Exception:
                pass
            setattr(_TLS, key, None)

    con = _new_connection(readonly=readonly)
    setattr(_TLS, key, con)
    return con

def connect_ro() -> sqlite3.Connection:
    return connect(readonly=True)

def _maybe_wal_checkpoint(con: sqlite3.Connection, *, force: bool = False) -> None:
    """
    Batched WAL checkpoints to keep WAL size bounded without constant stalls.
    Triggered by write volume and/or elapsed time.
    """
    global _LAST_CKPT_MS, _WRITE_SINCE_CKPT

    now_ms = int(time.time() * 1000)
    due_time = (now_ms - int(_LAST_CKPT_MS)) >= int(_WAL_CKPT_EVERY_S * 1000)
    due_writes = _WRITE_SINCE_CKPT >= int(max(1, _WAL_CKPT_EVERY_WRITES))

    if not force and not (due_time or due_writes):
        return

    mode = _WAL_CKPT_MODE
    if mode not in ("PASSIVE", "RESTART", "TRUNCATE"):
        mode = "PASSIVE"

    try:
        con.execute(f"PRAGMA wal_checkpoint({mode});").fetchall()
    except Exception:
        try:
            con.execute("PRAGMA wal_checkpoint(PASSIVE);").fetchall()
        except Exception:
            pass

    _LAST_CKPT_MS = now_ms
    _WRITE_SINCE_CKPT = 0


def _maybe_quick_check(con: sqlite3.Connection) -> None:
    """
    Practical corruption hardening: periodic quick_check on the write connection.
    Disabled if SQLITE_QUICK_CHECK_EVERY_S=0.
    """
    global _LAST_QUICK_CHECK_MS
    if _QUICK_CHECK_EVERY_S <= 0:
        return

    now_ms = int(time.time() * 1000)
    if _LAST_QUICK_CHECK_MS and (now_ms - int(_LAST_QUICK_CHECK_MS)) < int(_QUICK_CHECK_EVERY_S * 1000):
        return

    try:
        con.execute("PRAGMA quick_check;").fetchone()
        _LAST_QUICK_CHECK_MS = now_ms
    except Exception:
        pass


def _note_write(con: sqlite3.Connection) -> None:
    """
    Called after successful writes to increment counters and maybe checkpoint.
    """
    global _WRITE_SINCE_CKPT
    _WRITE_SINCE_CKPT += 1
    _maybe_quick_check(con)
    _maybe_wal_checkpoint(con, force=False)


def _has_column(con, table: str, col: str) -> bool:

    rows = con.execute(f"PRAGMA table_info({table});").fetchall()
    return any(str(r[1]).lower() == col.lower() for r in rows)

def _ensure_labels_columns(con):
    # Migration for older DBs
    if not _has_column(con, "labels", "vol_proxy"):
        con.execute("ALTER TABLE labels ADD COLUMN vol_proxy REAL;")
    if not _has_column(con, "labels", "regime"):
        con.execute("ALTER TABLE labels ADD COLUMN regime TEXT;")

def _ensure_price_quotes_schema(con):
    # Additive, idempotent: real-time quotes/spread/volume snapshots
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS price_quotes (
          ts_ms INTEGER NOT NULL,
          symbol TEXT NOT NULL,
          last REAL,
          bid REAL,
          ask REAL,
          spread REAL,
          volume REAL,
          source TEXT,
          PRIMARY KEY(symbol, ts_ms)
        );

        CREATE INDEX IF NOT EXISTS idx_price_quotes_symbol_ts
          ON price_quotes(symbol, ts_ms);

        CREATE INDEX IF NOT EXISTS idx_price_quotes_ts
          ON price_quotes(ts_ms);
        """
    )


def _ensure_price_quotes_raw_schema(con):
    # Additive, idempotent: per-provider raw snapshots (before ensemble)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS price_quotes_raw (
          ts_ms INTEGER NOT NULL,
          symbol TEXT NOT NULL,
          provider TEXT NOT NULL,
          last REAL,
          bid REAL,
          ask REAL,
          spread REAL,
          volume REAL,
          PRIMARY KEY(symbol, provider, ts_ms)
        );

        CREATE INDEX IF NOT EXISTS idx_price_quotes_raw_symbol_ts
          ON price_quotes_raw(symbol, ts_ms);

        CREATE INDEX IF NOT EXISTS idx_price_quotes_raw_provider_ts
          ON price_quotes_raw(provider, ts_ms);

        CREATE INDEX IF NOT EXISTS idx_price_quotes_raw_ts
          ON price_quotes_raw(ts_ms);
        """
    )

def _ensure_price_anomaly_schema(con):
    # Cross-provider spread divergence detection
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS price_anomalies (
          ts_ms INTEGER NOT NULL,
          symbol TEXT NOT NULL,
          provider_a TEXT,
          provider_b TEXT,
          spread_diff_bps REAL,
          reason TEXT,
          PRIMARY KEY(symbol, ts_ms)
        );

        CREATE INDEX IF NOT EXISTS idx_price_anomalies_ts
          ON price_anomalies(ts_ms);

        CREATE INDEX IF NOT EXISTS idx_price_anomalies_symbol
          ON price_anomalies(symbol);
        """
    )

def _ensure_options_chain_v2_schema(con):
    # Additive, idempotent: richer options snapshot (Polygon/Tradier/etc)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS options_chain_v2 (
          ts_ms INTEGER NOT NULL,

          underlying TEXT NOT NULL,
          contract TEXT NOT NULL,
          expiration TEXT,
          contract_type TEXT,
          strike REAL,

          iv REAL,
          open_interest REAL,
          volume REAL,

          bid REAL,
          ask REAL,

          delta REAL,
          gamma REAL,
          theta REAL,
          vega REAL,

          source TEXT,
          PRIMARY KEY(contract, ts_ms)
        );

        CREATE INDEX IF NOT EXISTS idx_options_chain_v2_under_ts
          ON options_chain_v2(underlying, ts_ms);

        CREATE INDEX IF NOT EXISTS idx_options_chain_v2_under_exp
          ON options_chain_v2(underlying, expiration);

        CREATE INDEX IF NOT EXISTS idx_options_chain_v2_ts
          ON options_chain_v2(ts_ms);
        """
    )

def _ensure_options_chain_schema(con):
    # Additive, idempotent: options IV / OI snapshots
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS options_chain (
          ts_ms INTEGER NOT NULL,
          symbol TEXT NOT NULL,
          expiry TEXT NOT NULL,
          strike REAL NOT NULL,
          call_put TEXT NOT NULL,          -- 'C' or 'P'
          iv REAL,
          open_interest INTEGER,
          volume INTEGER,
          source TEXT,
          PRIMARY KEY(symbol, expiry, strike, call_put, ts_ms)
        );

        CREATE INDEX IF NOT EXISTS idx_options_chain_symbol_ts
          ON options_chain(symbol, ts_ms);

        CREATE INDEX IF NOT EXISTS idx_options_chain_symbol_expiry
          ON options_chain(symbol, expiry);
        """
    )

def _ensure_earnings_calendar_schema(con):
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS earnings_calendar (
          symbol TEXT NOT NULL,
          earnings_date TEXT NOT NULL,     -- YYYY-MM-DD
          time_of_day TEXT,                -- 'bmo','amc','dmt','unknown'
          eps_est REAL,
          eps_act REAL,
          revenue_est REAL,
          revenue_act REAL,
          source TEXT,
          updated_ts_ms INTEGER NOT NULL,
          PRIMARY KEY(symbol, earnings_date)
        );

        CREATE INDEX IF NOT EXISTS idx_earnings_cal_symbol_date
          ON earnings_calendar(symbol, earnings_date);

        CREATE INDEX IF NOT EXISTS idx_earnings_cal_date
          ON earnings_calendar(earnings_date);
        """
    )


def _ensure_sec_filings_schema(con):
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS sec_filings (
          symbol TEXT NOT NULL,
          accession TEXT NOT NULL,
          form TEXT NOT NULL,
          filed_date TEXT NOT NULL,        -- YYYY-MM-DD
          report_date TEXT,                -- YYYY-MM-DD (optional)
          cik TEXT,
          company_name TEXT,
          primary_doc_url TEXT,
          items_json TEXT,
          source TEXT,
          ts_ms INTEGER NOT NULL,
          PRIMARY KEY(symbol, accession)
        );

        CREATE INDEX IF NOT EXISTS idx_sec_filings_symbol_ts
          ON sec_filings(symbol, ts_ms);

        CREATE INDEX IF NOT EXISTS idx_sec_filings_filed
          ON sec_filings(filed_date);

        CREATE INDEX IF NOT EXISTS idx_sec_filings_form
          ON sec_filings(form);
        """
    )

def _ensure_domain_blacklist_schema(con):
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS domain_blacklist (
          domain TEXT NOT NULL,
          symbol TEXT NOT NULL DEFAULT '*',     -- '*' means global
          status TEXT NOT NULL DEFAULT 'BLOCK', -- BLOCK or ALLOW
          score REAL,                          -- negative => bad
          reason TEXT,
          updated_ts_ms INTEGER NOT NULL,
          PRIMARY KEY(domain, symbol)
        );

        CREATE INDEX IF NOT EXISTS idx_domain_blacklist_symbol
          ON domain_blacklist(symbol);

        CREATE INDEX IF NOT EXISTS idx_domain_blacklist_updated
          ON domain_blacklist(updated_ts_ms);
        """
    )


def _ensure_domain_perf_schema(con):
    # performance stats computed from your real outcomes (labels/pnl attribution)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS domain_perf (
          ts_ms INTEGER NOT NULL,
          domain TEXT NOT NULL,
          symbol TEXT NOT NULL,
          regime TEXT NOT NULL,
          horizon_s INTEGER NOT NULL,

          n INTEGER NOT NULL,
          win_rate REAL,
          mean_edge REAL,           -- your chosen edge metric (computed later)
          mean_pnl REAL,            -- if you have realized pnl attribution
          mean_slip_bps REAL,       -- if you have realized slippage attribution

          PRIMARY KEY(domain, symbol, regime, horizon_s)
        );

        CREATE INDEX IF NOT EXISTS idx_domain_perf_domain
          ON domain_perf(domain);

        CREATE INDEX IF NOT EXISTS idx_domain_perf_symbol
          ON domain_perf(symbol);

        CREATE INDEX IF NOT EXISTS idx_domain_perf_ts
          ON domain_perf(ts_ms);
        """
    )

def _ensure_events_columns(con):
    # Optional metadata blob for features like novelty, source enrichments, etc.
    if not _has_column(con, "events", "meta_json"):
        con.execute("ALTER TABLE events ADD COLUMN meta_json TEXT;")

def _ensure_symbol_universe_columns(con):
    # Additive migration for symbol_universe
    if not _has_column(con, "symbol_universe", "last_demoted_ms"):
        con.execute("ALTER TABLE symbol_universe ADD COLUMN last_demoted_ms INTEGER;")

def _ensure_promotion_audit_columns(con):
    # Additive migration: ensure model_promotion_audit.regime exists
    try:
        if _has_column(con, "model_promotion_audit", "regime"):
            return
        con.execute("ALTER TABLE model_promotion_audit ADD COLUMN regime TEXT;")
    except Exception:
        pass


def _ensure_promotion_watch_schema(con):
    # Additive, idempotent migrations for post-promotion watch + results
    con.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_model_promo_audit_ts
          ON model_promotion_audit(ts_ms);

        CREATE INDEX IF NOT EXISTS idx_model_promo_audit_name_regime_ts
          ON model_promotion_audit(model_name, regime, ts_ms);

        CREATE TABLE IF NOT EXISTS model_promotion_cooldown (
          model_name TEXT NOT NULL,
          regime TEXT NOT NULL DEFAULT 'global',
          cooldown_until_ts_ms INTEGER NOT NULL,
          reason TEXT,
          updated_ts_ms INTEGER NOT NULL,
          PRIMARY KEY (model_name, regime)
        );

        CREATE INDEX IF NOT EXISTS idx_model_promo_cooldown_until
          ON model_promotion_cooldown(cooldown_until_ts_ms);

        CREATE TABLE IF NOT EXISTS model_post_promo_watch (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts_ms INTEGER NOT NULL,
          model_name TEXT NOT NULL,
          regime TEXT NOT NULL DEFAULT 'global',
          from_model_kind TEXT,
          from_model_ts_ms INTEGER,
          to_model_kind TEXT NOT NULL,
          to_model_ts_ms INTEGER NOT NULL,
          watch_until_ts_ms INTEGER NOT NULL,
          baseline_metrics_json TEXT,
          status TEXT NOT NULL DEFAULT 'active',
          last_eval_ts_ms INTEGER,
          note TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_model_post_watch_active
          ON model_post_promo_watch(status, watch_until_ts_ms);

        CREATE TABLE IF NOT EXISTS model_post_promo_results (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          watch_id INTEGER NOT NULL,
          ts_ms INTEGER NOT NULL,
          n INTEGER NOT NULL,
          rmse REAL,
          dir_acc REAL,
          net_rmse REAL,
          net_dir_acc REAL,
          extra_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_model_post_results_watch_ts
          ON model_post_promo_results(watch_id, ts_ms);
        """
    )


def _ensure_strategy_metrics_schema(con):
    # Additive, idempotent: strategy evaluation metrics used by meta-strategy selection
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS strategy_metrics (
          strategy_name TEXT NOT NULL,
          window_days INTEGER NOT NULL,
          ts_ms INTEGER NOT NULL,
          metrics_json TEXT NOT NULL,
          PRIMARY KEY (strategy_name, window_days)
        );

        CREATE INDEX IF NOT EXISTS idx_strategy_metrics_ts
          ON strategy_metrics(ts_ms);

        CREATE TABLE IF NOT EXISTS strategy_allocations (
          ts_ms INTEGER NOT NULL,
          window_days INTEGER NOT NULL,
          allocations_json TEXT NOT NULL,
          reason_json TEXT,
          PRIMARY KEY (ts_ms, window_days)
        );

        CREATE INDEX IF NOT EXISTS idx_strategy_allocations_ts
          ON strategy_allocations(ts_ms);

        CREATE TABLE IF NOT EXISTS strategy_registry (
          strategy_name TEXT PRIMARY KEY,
          enabled INTEGER NOT NULL DEFAULT 1,
          stage TEXT NOT NULL DEFAULT 'paper',   -- paper|shadow|live
          created_ts_ms INTEGER NOT NULL,
          updated_ts_ms INTEGER NOT NULL,
          meta_json TEXT
        );
        """
    )

    # Optional column used by governance to mark pass/fail states.
    # Additive migration; safe on existing DBs.
    try:
        if not _has_column(con, "strategy_metrics", "is_active"):
            con.execute("ALTER TABLE strategy_metrics ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;")
    except Exception:
        pass

def _ensure_universe_audit_schema(con):
    # Additive, idempotent: universe selection / exclusion reasons for auditability
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

def _ensure_execution_mode_armed_column(con):
    # Additive migration: execution_mode.armed for explicit live arming in DB (single source of truth)
    try:
        if not _has_column(con, "execution_mode", "armed"):
            con.execute("ALTER TABLE execution_mode ADD COLUMN armed INTEGER NOT NULL DEFAULT 0;")
    except Exception:
        pass

def _ensure_kill_switch_schema(con):
    # Additive, idempotent migrations for kill-switch tables / indexes
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS kill_switch_state (
          scope TEXT NOT NULL,
          key TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 0,
          reason TEXT,
          actor TEXT NOT NULL DEFAULT 'system',
          meta_json TEXT,
          created_ts_ms INTEGER NOT NULL,
          updated_ts_ms INTEGER NOT NULL,
          PRIMARY KEY (scope, key)
        );

        CREATE INDEX IF NOT EXISTS idx_kill_switch_scope_enabled
          ON kill_switch_state(scope, enabled);

        CREATE TABLE IF NOT EXISTS kill_switch_audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts_ms INTEGER NOT NULL,
          action TEXT NOT NULL,
          scope TEXT NOT NULL,
          key TEXT NOT NULL,
          enabled INTEGER NOT NULL,
          actor TEXT NOT NULL,
          reason TEXT,
          meta_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_kill_switch_audit_ts
          ON kill_switch_audit(ts_ms);
        """
    )


def _ensure_trade_attribution_ledger_schema(con):
    # Additive, idempotent: trade attribution ledger (signal/model/regime/policy/suppression -> pnl)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS trade_attribution_ledger (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts_ms INTEGER NOT NULL,

          source_alert_id INTEGER,
          symbol TEXT NOT NULL,

          signal_json TEXT,
          model_json TEXT,
          regime_vector_json TEXT,

          execution_policy_json TEXT,
          suppression_reason TEXT,

          pnl REAL,
          fees REAL,
          slippage_bps REAL,

          decision_json TEXT,
          created_ts_ms INTEGER NOT NULL,

          UNIQUE(ts_ms, source_alert_id, symbol, suppression_reason)
        );

        CREATE INDEX IF NOT EXISTS idx_trade_attr_ts
          ON trade_attribution_ledger(ts_ms);

        CREATE INDEX IF NOT EXISTS idx_trade_attr_alert
          ON trade_attribution_ledger(source_alert_id);

        CREATE INDEX IF NOT EXISTS idx_trade_attr_symbol_ts
          ON trade_attribution_ledger(symbol, ts_ms);
        """
    )

def _ensure_shadow_capital_schema(con):
    # Additive, idempotent: shadow capital scoring snapshots (model-level governance)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS shadow_capital_scores (
          ts_ms INTEGER NOT NULL,
          window_s INTEGER NOT NULL,
          regime TEXT NOT NULL DEFAULT 'global',

          model_name TEXT NOT NULL,
          model_kind TEXT,
          model_ts_ms INTEGER,

          n INTEGER NOT NULL DEFAULT 0,

          -- quality
          rmse REAL,
          dir_acc REAL,
          net_rmse REAL,

          -- costs / risk
          slippage_bps_mean REAL,
          slippage_bps_std REAL,
          drawdown_proxy REAL,

          -- capital efficiency (net edge per unit cost proxy)
          cap_eff REAL,

          -- composite governance score (higher is better)
          score REAL NOT NULL,

          -- debug/audit
          weights_json TEXT,
          components_json TEXT,

          PRIMARY KEY (model_name, window_s, regime)
        );

        CREATE INDEX IF NOT EXISTS idx_shadow_capital_scores_ts
          ON shadow_capital_scores(ts_ms);

        CREATE INDEX IF NOT EXISTS idx_shadow_capital_scores_score
          ON shadow_capital_scores(score);

        CREATE INDEX IF NOT EXISTS idx_shadow_capital_scores_regime_score
          ON shadow_capital_scores(regime, score);
        """
    )

def init_db():
    con = connect(readonly=False)
    try:

        con.executescript(
            """
            -- -            -- ------------------------------------------------------
            -- Core ingestion
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts_ms INTEGER NOT NULL,
              source TEXT NOT NULL,
              title TEXT NOT NULL,
              body TEXT,
              url TEXT,
              meta_json TEXT,
              event_key TEXT UNIQUE
            );

            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_ms);
            CREATE INDEX IF NOT EXISTS idx_events_source_ts ON events(source, ts_ms);

            -- -            -- ------------------------------------------------------
            -- GDELT macro narrative features (market-wide, bucketed)
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS gdelt_macro_features (
              bucket_ts_ms INTEGER NOT NULL,
              bucket_sec INTEGER NOT NULL,
              doc_count INTEGER NOT NULL DEFAULT 0,
              tone_mean REAL NOT NULL DEFAULT 0.0,
              tone_std REAL NOT NULL DEFAULT 0.0,
              conflict_share REAL NOT NULL DEFAULT 0.0,
              econ_share REAL NOT NULL DEFAULT 0.0,
              PRIMARY KEY(bucket_ts_ms, bucket_sec)
            );

            CREATE INDEX IF NOT EXISTS idx_gdelt_macro_bucket
              ON gdelt_macro_features(bucket_ts_ms);

            -- -            -- ------------------------------------------------------
            -- Social (raw posts + bucketed features + regime labels)
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS social_posts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts_ms INTEGER NOT NULL,
              platform TEXT NOT NULL,
              symbol TEXT NOT NULL,
              post_id TEXT NOT NULL,
              author_id_hash TEXT,
              text TEXT,
              lang TEXT,
              like_count INTEGER,
              reply_count INTEGER,
              repost_count INTEGER,
              quote_count INTEGER,
              follower_count INTEGER,
              is_spam INTEGER NOT NULL DEFAULT 0,
              spam_reason TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_social_posts_platform_post
              ON social_posts(platform, post_id);

            CREATE INDEX IF NOT EXISTS idx_social_posts_symbol_ts
              ON social_posts(symbol, ts_ms);

            CREATE INDEX IF NOT EXISTS idx_social_posts_ts
              ON social_posts(ts_ms);

            CREATE TABLE IF NOT EXISTS social_features (
              symbol TEXT NOT NULL,
              bucket_ts_ms INTEGER NOT NULL,
              bucket_sec INTEGER NOT NULL,
              mention_count INTEGER NOT NULL DEFAULT 0,
              unique_authors INTEGER NOT NULL DEFAULT 0,
              new_author_ratio REAL NOT NULL DEFAULT 0.0,
              engagement_now REAL NOT NULL DEFAULT 0.0,
              sentiment_mean REAL NOT NULL DEFAULT 0.0,
              sentiment_dispersion REAL NOT NULL DEFAULT 0.0,
              mention_rate_z REAL NOT NULL DEFAULT 0.0,
              bot_likelihood_mean REAL NOT NULL DEFAULT 0.0,
              promo_likelihood_mean REAL NOT NULL DEFAULT 0.0,
              manip_risk REAL NOT NULL DEFAULT 0.0,
              attention_shock REAL NOT NULL DEFAULT 0.0,
              cross_platform_confirm REAL NOT NULL DEFAULT 0.0,
              PRIMARY KEY(symbol, bucket_ts_ms, bucket_sec)
            );

            CREATE INDEX IF NOT EXISTS idx_social_features_symbol_bucket
              ON social_features(symbol, bucket_ts_ms);

            CREATE INDEX IF NOT EXISTS idx_social_features_bucket
              ON social_features(bucket_ts_ms);

            CREATE TABLE IF NOT EXISTS social_regimes (
              symbol TEXT NOT NULL,
              bucket_ts_ms INTEGER NOT NULL,
              bucket_sec INTEGER NOT NULL,
              regime TEXT NOT NULL,                 -- QUIET | CHURN | FEAR | MANIA
              regime_conf REAL NOT NULL DEFAULT 0.0,
              mania_score REAL NOT NULL DEFAULT 0.0,
              fear_score REAL NOT NULL DEFAULT 0.0,
              churn_score REAL NOT NULL DEFAULT 0.0,
              PRIMARY KEY(symbol, bucket_ts_ms, bucket_sec)
            );

            CREATE INDEX IF NOT EXISTS idx_social_regimes_symbol_bucket
              ON social_regimes(symbol, bucket_ts_ms);

            CREATE TABLE IF NOT EXISTS prices (
              ts_ms INTEGER NOT NULL,
              symbol TEXT NOT NULL,
              price REAL NOT NULL,
              PRIMARY KEY(symbol, ts_ms)
            );

            CREATE INDEX IF NOT EXISTS idx_prices_symbol_ts
              ON prices(symbol, ts_ms);
            -- -            -- ------------------------------------------------------
            -- Optional: computed market/tech features (versioned JSON)
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS market_features (
              ts_ms INTEGER NOT NULL,
              symbol TEXT NOT NULL,
              v INTEGER NOT NULL,
              features_json TEXT NOT NULL,
              PRIMARY KEY(symbol, ts_ms, v)
            );

            CREATE INDEX IF NOT EXISTS idx_market_features_symbol_ts
              ON market_features(symbol, ts_ms);

            -- ------------------------------------------------------
            -- Provider health (for auto-failover)
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS price_provider_health (
              ts_ms INTEGER NOT NULL,
              provider TEXT NOT NULL,
              ok INTEGER NOT NULL,
              latency_ms INTEGER,
              n_symbols INTEGER,
              error TEXT,
              PRIMARY KEY(provider, ts_ms)
            );

            CREATE INDEX IF NOT EXISTS idx_price_provider_health_ts
              ON price_provider_health(ts_ms);

            CREATE INDEX IF NOT EXISTS idx_price_provider_health_provider
              ON price_provider_health(provider);

            -- -            -- ------------------------------------------------------
            -- Ingest-side slippage proxy (mid vs last) per provider
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS ingest_slippage (
              ts_ms INTEGER NOT NULL,
              symbol TEXT NOT NULL,
              provider TEXT NOT NULL,
              last REAL,
              bid REAL,
              ask REAL,
              mid REAL,
              spread REAL,
              px_minus_mid REAL,
              abs_px_minus_mid REAL,
              PRIMARY KEY(symbol, provider, ts_ms)
            );

            CREATE INDEX IF NOT EXISTS idx_ingest_slippage_ts
              ON ingest_slippage(ts_ms);

            CREATE INDEX IF NOT EXISTS idx_ingest_slippage_symbol
              ON ingest_slippage(symbol);

            CREATE INDEX IF NOT EXISTS idx_ingest_slippage_provider
              ON ingest_slippage(provider);

            -- -            -- ------------------------------------------------------
            -- Dynamic symbol universe (WATCH → ACTIVE → BLOCKED)
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS symbol_universe (
              symbol TEXT PRIMARY KEY,
              status TEXT NOT NULL,              -- WATCH | ACTIVE | BLOCKED
              first_seen_ms INTEGER NOT NULL,
              last_seen_ms INTEGER NOT NULL,
              last_promoted_ms INTEGER,
              last_demoted_ms INTEGER,
              seen_n INTEGER NOT NULL DEFAULT 1,
              meta_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_symbol_universe_status
              ON symbol_universe(status);

            -- -            -- ------------------------------------------------------
            -- OHLCV bars (for tradability + correlation + risk)
            -- tf_s: timeframe in seconds (e.g. 60 for 1m)
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS price_bars (
              tf_s INTEGER NOT NULL,
              ts_ms INTEGER NOT NULL,
              symbol TEXT NOT NULL,
              o REAL NOT NULL,
              h REAL NOT NULL,
              l REAL NOT NULL,
              c REAL NOT NULL,
              v REAL,
              PRIMARY KEY(symbol, tf_s, ts_ms)
            );

            CREATE INDEX IF NOT EXISTS idx_price_bars_symbol_tf_ts
              ON price_bars(symbol, tf_s, ts_ms);

            -- -            -- ------------------------------------------------------
            -- Symbol registry (dynamic universe)
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS symbols (
              symbol TEXT PRIMARY KEY,
              asset_class TEXT NOT NULL DEFAULT 'UNKNOWN',
              status TEXT NOT NULL DEFAULT 'WATCH',   -- WATCH / ACTIVE / COOLDOWN / DISABLED
              score REAL NOT NULL DEFAULT 0.0,        -- universe score (higher = more relevant)
              last_seen_event_ts_ms INTEGER,
              last_traded_ts_ms INTEGER,
              meta_json TEXT,
              created_ts_ms INTEGER NOT NULL,
              updated_ts_ms INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_symbols_status_score
              ON symbols(status, score);

            CREATE INDEX IF NOT EXISTS idx_symbols_updated
              ON symbols(updated_ts_ms);

            -- -            -- ------------------------------------------------------
            -- Labels
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS labels (
              event_id INTEGER NOT NULL,
              horizon_s INTEGER NOT NULL,
              symbol TEXT NOT NULL,
              baseline_ret REAL,
              realized_ret REAL,
              impact_z REAL,
              created_at_ms INTEGER,
              vol_proxy REAL,
              regime TEXT,
              UNIQUE(event_id, symbol, horizon_s)
            );

            CREATE INDEX IF NOT EXISTS idx_labels_symbol_h
              ON labels(symbol, horizon_s);

            -- -            -- ------------------------------------------------------
            -- Embeddings
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS event_embeddings (
              event_id INTEGER PRIMARY KEY,
              dim INTEGER NOT NULL,
              vec BLOB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS event_embeddings_seq (
              event_id INTEGER PRIMARY KEY,
              dim INTEGER NOT NULL,
              vec BLOB NOT NULL
            );

            -- -            -- ------------------------------------------------------
            -- Equity / risk state
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS equity_history (
              ts_ms INTEGER PRIMARY KEY,
              equity REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS risk_state (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_ts_ms INTEGER NOT NULL
            );

            -- -            -- ------------------------------------------------------
            -- Job locks + heartbeats (production stability)
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS job_locks (
              job_name TEXT PRIMARY KEY,
              owner TEXT NOT NULL,
              pid INTEGER NOT NULL,
              acquired_ts_ms INTEGER NOT NULL,
              heartbeat_ts_ms INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS job_heartbeats (
              job_name TEXT PRIMARY KEY,
              owner TEXT NOT NULL,
              pid INTEGER NOT NULL,
              ts_ms INTEGER NOT NULL,
              extra_json TEXT
            );

                        -- ------------------------------------------------------
            -- Crash-safe resume checkpoints (idempotent)
            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS job_checkpoints (
              job_name TEXT PRIMARY KEY,
              last_event_id INTEGER,
              last_event_ts_ms INTEGER,
              updated_ts_ms INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_job_checkpoints_updated
              ON job_checkpoints(updated_ts_ms);

            -- -            -- ------------------------------------------------------
            -- Temporal models
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS temporal_models (
              key_type TEXT NOT NULL,
              key TEXT NOT NULL,
              horizon_s INTEGER NOT NULL,
              ts_ms INTEGER NOT NULL,
              n INTEGER NOT NULL,
              embed_dim INTEGER NOT NULL,
              seq_len INTEGER NOT NULL,
              model_kind TEXT NOT NULL,
              model_blob BLOB NOT NULL,
              PRIMARY KEY (key_type, key, horizon_s)
            );

            CREATE TABLE IF NOT EXISTS temporal_model_eval (
              key_type TEXT NOT NULL,
              key TEXT NOT NULL,
              horizon_s INTEGER NOT NULL,
              model_kind TEXT NOT NULL,
              ts_ms INTEGER NOT NULL,
              n_train INTEGER NOT NULL,
              n_eval INTEGER NOT NULL,
              rmse REAL NOT NULL,
              spearman REAL NOT NULL,
              directional_acc REAL NOT NULL,
              PRIMARY KEY (key_type, key, horizon_s, model_kind)
            );

            CREATE TABLE IF NOT EXISTS temporal_predictions (
              event_id INTEGER NOT NULL,
              symbol TEXT NOT NULL,
              horizon_s INTEGER NOT NULL,
              model_kind TEXT NOT NULL,
              expected_z REAL NOT NULL,
              confidence REAL NOT NULL,
              explain_json TEXT,
              ts_ms INTEGER NOT NULL,
              PRIMARY KEY (event_id, symbol, horizon_s, model_kind)
            );
            -- -            -- ------------------------------------------------------
            -- Weather model contribution (base vs weather)
            -- -            -- ------------------------------------------------------


            -- -            -- ------------------------------------------------------
            -- Drift & backtests
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS model_drift (
              symbol TEXT NOT NULL,
              horizon_s INTEGER NOT NULL,
              ts_ms INTEGER NOT NULL,
              n INTEGER NOT NULL,
              mae REAL NOT NULL,
              baseline_mae REAL NOT NULL,
              drift_ratio REAL NOT NULL,
              PRIMARY KEY(symbol, horizon_s)
            );

            CREATE TABLE IF NOT EXISTS backtest_scores (
              symbol TEXT NOT NULL,
              horizon_s INTEGER NOT NULL,
              ts_ms INTEGER NOT NULL,
              n INTEGER NOT NULL,
              mae REAL NOT NULL,
              dir_acc REAL NOT NULL,
              PRIMARY KEY(symbol, horizon_s)
            );

            -- -            -- ------------------------------------------------------
            -- Model registry (single canonical definition)
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS model_registry (
              model_name TEXT NOT NULL,
              model_kind TEXT NOT NULL,
              model_ts_ms INTEGER NOT NULL,
              stage TEXT NOT NULL,
              regime TEXT NOT NULL DEFAULT 'global',
              metrics_json TEXT NOT NULL,
              created_ts_ms INTEGER NOT NULL,
              note TEXT,
              PRIMARY KEY (model_name, model_kind, model_ts_ms)
            );

            CREATE TABLE IF NOT EXISTS model_promotion_audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts_ms INTEGER NOT NULL,
              actor TEXT NOT NULL,
              action TEXT NOT NULL,
              model_name TEXT NOT NULL,
              from_model_kind TEXT,
              from_model_ts_ms INTEGER,
              to_model_kind TEXT,
              to_model_ts_ms INTEGER,
              reason_json TEXT,
              regime TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_model_promo_audit_ts
              ON model_promotion_audit(ts_ms);

            CREATE INDEX IF NOT EXISTS idx_model_promo_audit_name_regime_ts
              ON model_promotion_audit(model_name, regime, ts_ms);

            CREATE TABLE IF NOT EXISTS model_promotion_cooldown (
              model_name TEXT NOT NULL,
              regime TEXT NOT NULL DEFAULT 'global',
              cooldown_until_ts_ms INTEGER NOT NULL,
              reason TEXT,
              updated_ts_ms INTEGER NOT NULL,
              PRIMARY KEY (model_name, regime)
            );

            CREATE INDEX IF NOT EXISTS idx_model_promo_cooldown_until
              ON model_promotion_cooldown(cooldown_until_ts_ms);

            CREATE TABLE IF NOT EXISTS model_post_promo_watch (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts_ms INTEGER NOT NULL,
              model_name TEXT NOT NULL,
              regime TEXT NOT NULL DEFAULT 'global',
              from_model_kind TEXT,
              from_model_ts_ms INTEGER,
              to_model_kind TEXT NOT NULL,
              to_model_ts_ms INTEGER NOT NULL,
              watch_until_ts_ms INTEGER NOT NULL,
              baseline_metrics_json TEXT,
              status TEXT NOT NULL DEFAULT 'active',
              last_eval_ts_ms INTEGER,
              note TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_model_post_watch_active
              ON model_post_promo_watch(status, watch_until_ts_ms);

            CREATE TABLE IF NOT EXISTS model_post_promo_results (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              watch_id INTEGER NOT NULL,
              ts_ms INTEGER NOT NULL,
              n INTEGER NOT NULL,
              rmse REAL,
              dir_acc REAL,
              net_rmse REAL,
              net_dir_acc REAL,
              extra_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_model_post_results_watch_ts
              ON model_post_promo_results(watch_id, ts_ms);

            CREATE TABLE IF NOT EXISTS model_promotion_guard (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_ts_ms INTEGER NOT NULL
            );


            -- -            -- ------------------------------------------------------
            -- Execution-aware labels (FIXED)
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS labels_exec (
              event_id INTEGER NOT NULL,
              symbol TEXT NOT NULL,
              horizon_s INTEGER NOT NULL,
              ts_ms INTEGER NOT NULL,

              source TEXT NOT NULL DEFAULT 'heuristic',
              realized INTEGER NOT NULL DEFAULT 0,

              side INTEGER NOT NULL,
              gross_ret REAL NOT NULL,
              net_ret REAL NOT NULL,
              gross_z REAL,
              net_z REAL,

              mid_in REAL,
              mid_out REAL,
              spread_in REAL,
              fees_bps REAL NOT NULL,
              slippage_bps REAL NOT NULL,
              spread_bps REAL NOT NULL,
              total_cost_bps REAL NOT NULL,

              extra_json TEXT,
              PRIMARY KEY (event_id, symbol, horizon_s)
            );

            -- -            -- ------------------------------------------------------
            -- Kill switches (system-wide, fail-closed)
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS kill_switch_state (
              scope TEXT NOT NULL,                 -- global / symbol / regime
              key TEXT NOT NULL,                   -- 'global' or '<SYMBOL>' or '<REGIME>'
              enabled INTEGER NOT NULL DEFAULT 0,  -- 0/1
              reason TEXT,
              actor TEXT NOT NULL DEFAULT 'system',
              meta_json TEXT,
              created_ts_ms INTEGER NOT NULL,
              updated_ts_ms INTEGER NOT NULL,
              PRIMARY KEY (scope, key)
            );

            CREATE INDEX IF NOT EXISTS idx_kill_switch_scope_enabled
              ON kill_switch_state(scope, enabled);

            CREATE TABLE IF NOT EXISTS kill_switch_audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts_ms INTEGER NOT NULL,
              action TEXT NOT NULL,                -- SET / CLEAR / AUTO
              scope TEXT NOT NULL,
              key TEXT NOT NULL,
              enabled INTEGER NOT NULL,
              actor TEXT NOT NULL,
              reason TEXT,
              meta_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_kill_switch_audit_ts
              ON kill_switch_audit(ts_ms);

            -- -            -- ------------------------------------------------------
            -- Shadow training runs
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS shadow_training_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts_ms INTEGER NOT NULL,
              model_name TEXT NOT NULL,
              regime TEXT,
              horizon_s INTEGER NOT NULL,
              train_rows INTEGER NOT NULL,
              metrics_json TEXT,
              status TEXT NOT NULL,
              error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_shadow_training_ts
              ON shadow_training_runs(ts_ms);

            -- -            -- ------------------------------------------------------
            -- Shadow metrics (non-executing)
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS shadow_predictions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts_ms INTEGER NOT NULL,
              event_id INTEGER NOT NULL,
              symbol TEXT NOT NULL,
              regime TEXT,
              horizon_s INTEGER NOT NULL,
              model_name TEXT NOT NULL,
              model_kind TEXT,
              model_ts_ms INTEGER,
              predicted_z REAL NOT NULL,
              confidence REAL NOT NULL,
              cost_est REAL,
              net_pred_z REAL,
              extra_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_shadow_predictions_ts
              ON shadow_predictions(ts_ms);

            CREATE TABLE IF NOT EXISTS shadow_metrics (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              window_start_ms INTEGER NOT NULL,
              window_end_ms INTEGER NOT NULL,
              regime TEXT,
              model_name TEXT NOT NULL,
              horizon_s INTEGER NOT NULL,
              rmse REAL,
              mae REAL,
              dir_acc REAL,
              avg_cost REAL,
              net_rmse REAL,
              n INTEGER NOT NULL,
              extra_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_shadow_metrics_window
              ON shadow_metrics(window_end_ms);

            -- -            -- ------------------------------------------------------
            -- Decision log
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS decision_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts_ms INTEGER NOT NULL,
              event_id INTEGER NOT NULL,
              symbol TEXT NOT NULL,
              horizon_s INTEGER NOT NULL,
              predicted_z REAL NOT NULL,
              confidence REAL NOT NULL,
              model_name TEXT NOT NULL,
              model_kind TEXT,
              model_ts_ms INTEGER,
              features_hash TEXT,
              features_json TEXT,
              explain_json TEXT,
              extra_json TEXT,
              UNIQUE(event_id, symbol, horizon_s, model_name, model_ts_ms)
            );

            -- -            -- ------------------------------------------------------
            -- Size policy (confidence -> sizing factor)
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS size_policy (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts_ms INTEGER NOT NULL,
              method TEXT NOT NULL,
              params_json TEXT NOT NULL,
              metrics_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_size_policy_ts
              ON size_policy(ts_ms);

            CREATE TABLE IF NOT EXISTS size_policy_points (
              policy_id INTEGER NOT NULL,
              bucket_idx INTEGER NOT NULL,
              conf_lo REAL NOT NULL,
              conf_hi REAL NOT NULL,
              n INTEGER NOT NULL,
              mean_net_ret REAL NOT NULL,
              std_net_ret REAL NOT NULL,
              factor REAL NOT NULL,
              PRIMARY KEY (policy_id, bucket_idx)
            );

            CREATE INDEX IF NOT EXISTS idx_size_policy_points_policy
              ON size_policy_points(policy_id, bucket_idx);

            -- Portfolio backtest outputs (required by dashboard preflight)
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS portfolio_bt_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts_ms INTEGER NOT NULL,
              start_ts_ms INTEGER NOT NULL,
              end_ts_ms INTEGER NOT NULL,
              metrics_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS portfolio_bt_points (
              run_id INTEGER NOT NULL,
              ts_ms INTEGER NOT NULL,
              ret REAL NOT NULL,
              equity REAL NOT NULL,
              drawdown REAL NOT NULL,
              exec_cost REAL DEFAULT 0.0,
              slippage REAL DEFAULT 0.0,
              fees REAL DEFAULT 0.0,
              detail_json TEXT,
              PRIMARY KEY (run_id, ts_ms)
            );

            CREATE INDEX IF NOT EXISTS idx_portfolio_bt_points_run
              ON portfolio_bt_points(run_id, ts_ms);

            CREATE INDEX IF NOT EXISTS idx_portfolio_bt_points_ts
              ON portfolio_bt_points(ts_ms);

            -- -            -- ------------------------------------------------------
            -- External factor universe (as-of, revision-safe)
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS factor_registry (
              factor_id TEXT PRIMARY KEY,
              family TEXT NOT NULL,
              name TEXT NOT NULL,
              cadence TEXT NOT NULL,
              release_lag_sec INTEGER DEFAULT 0,
              applies_to TEXT,
              units TEXT,
              transform TEXT,
              is_revisioned INTEGER NOT NULL DEFAULT 0,
              source TEXT,
              enabled INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS factor_observations (
              factor_id TEXT NOT NULL,
              asof_ts INTEGER NOT NULL,
              effective_ts INTEGER NOT NULL,
              value REAL,
              version INTEGER NOT NULL DEFAULT 1,
              meta_json TEXT,
              PRIMARY KEY (factor_id, asof_ts, effective_ts, version)
            );

            CREATE INDEX IF NOT EXISTS idx_factor_obs_factor_asof
              ON factor_observations(factor_id, asof_ts);

            CREATE INDEX IF NOT EXISTS idx_factor_obs_effective
              ON factor_observations(factor_id, effective_ts);

            CREATE TABLE IF NOT EXISTS factor_features (
              feature_id TEXT NOT NULL,
              asof_ts INTEGER NOT NULL,
              effective_ts INTEGER NOT NULL,
              value REAL,
              meta_json TEXT,
              PRIMARY KEY (feature_id, asof_ts, effective_ts)
            );

            CREATE INDEX IF NOT EXISTS idx_factor_features_feature_asof
              ON factor_features(feature_id, asof_ts);

            CREATE TABLE IF NOT EXISTS factor_groups (
              group_id TEXT PRIMARY KEY,
              description TEXT,
              members_json TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS factor_group_scores (
              ts INTEGER NOT NULL,
              scope TEXT NOT NULL,
              horizon TEXT NOT NULL,
              group_id TEXT NOT NULL,
              model_id TEXT NOT NULL,
              metric_ic REAL,
              metric_calibration REAL,
              metric_drawdown REAL,
              metric_turnover REAL,
              metric_cost REAL,
              metric_stability REAL,
              delta_vs_base REAL,
              decision TEXT,
              PRIMARY KEY (ts, scope, horizon, group_id, model_id)
            );

            CREATE INDEX IF NOT EXISTS idx_factor_group_scores_scope
              ON factor_group_scores(scope, horizon, ts);

            CREATE TABLE IF NOT EXISTS active_feature_policy (
              scope TEXT NOT NULL,
              horizon TEXT NOT NULL,
              group_id TEXT NOT NULL,
              weight REAL NOT NULL,
              state TEXT NOT NULL,
              since_ts INTEGER NOT NULL,
              PRIMARY KEY (scope, horizon, group_id)
            );

            CREATE INDEX IF NOT EXISTS idx_active_feature_policy_scope
              ON active_feature_policy(scope, horizon, weight);

            -- -            -- ------------------------------------------------------
            -- Weather forecasts (as-issued, leakage-safe)
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS weather_forecast_region_daily (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              provider TEXT NOT NULL,          -- 'open_meteo' | 'gfs' | 'ecmwf' | ...
              region_id TEXT NOT NULL,         -- e.g. 'us_pop', 'us_gulf', 'corn_belt'
              run_ts INTEGER NOT NULL,         -- forecast run/issue time (unix ms)
              day_ts INTEGER NOT NULL,         -- 00:00 UTC day start (unix ms)

              temp_mean_c REAL,
              hdd65 REAL,
              cdd65 REAL,
              wind_mean_mps REAL,
              precip_sum_mm REAL,
              spread REAL,

              source_uri TEXT,
              UNIQUE(provider, region_id, run_ts, day_ts)
            );

            CREATE INDEX IF NOT EXISTS idx_wx_region_lookup
              ON weather_forecast_region_daily(provider, region_id, day_ts, run_ts);

            -- -            -- ------------------------------------------------------
            -- Weather provider health (optional but recommended)
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS weather_provider_health (
              ts_ms INTEGER NOT NULL,
              provider TEXT NOT NULL,
              ok INTEGER NOT NULL,
              latency_ms INTEGER,
              error TEXT,
              PRIMARY KEY (provider, ts_ms)
            );

            CREATE INDEX IF NOT EXISTS idx_weather_provider_health_ts
              ON weather_provider_health(ts_ms);

            -- -            -- ------------------------------------------------------
            -- Weather alerts / events (event stream)
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS weather_alerts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              provider TEXT NOT NULL,          -- 'nws'
              alert_id TEXT NOT NULL,
              issued_ts INTEGER NOT NULL,
              effective_ts INTEGER,
              expires_ts INTEGER,

              event TEXT,
              severity TEXT,
              urgency TEXT,
              certainty TEXT,

              area_desc TEXT,
              polygon_geojson TEXT,
              affected_regions TEXT,           -- JSON list of region_ids
              headline TEXT,
              description TEXT,

              source_uri TEXT,
              UNIQUE(provider, alert_id)
            );

            CREATE INDEX IF NOT EXISTS idx_wx_alerts_time
              ON weather_alerts(provider, issued_ts);

            -- -            -- ------------------------------------------------------
            -- Weather usefulness tracking (base vs wx) per regime
            -- -            -- ------------------------------------------------------
            CREATE TABLE IF NOT EXISTS model_weather_effect (
              key_type TEXT NOT NULL,          -- 'symbol' | 'class'
              key TEXT NOT NULL,               -- raw key (NOT namespaced)
              horizon_s INTEGER NOT NULL,
              regime TEXT NOT NULL DEFAULT 'global',
              ts_ms INTEGER NOT NULL,

              base_rmse REAL,
              wx_rmse REAL,
              rmse_delta REAL,

              base_dir_acc REAL,
              wx_dir_acc REAL,
              dir_acc_delta REAL,

              n_eval INTEGER NOT NULL,
              PRIMARY KEY (key_type, key, horizon_s, regime, ts_ms)
            );

            CREATE INDEX IF NOT EXISTS idx_model_wx_effect_lookup
              ON model_weather_effect(key_type, key, horizon_s, regime, ts_ms);

            """
        )

        _ensure_labels_columns(con)
        _ensure_events_columns(con)
        _ensure_symbol_universe_columns(con)
        _ensure_price_quotes_schema(con)
        _ensure_price_quotes_raw_schema(con)
        _ensure_price_anomaly_schema(con)
        _ensure_options_chain_schema(con)
        _ensure_options_chain_v2_schema(con)
        _ensure_earnings_calendar_schema(con)
        _ensure_sec_filings_schema(con)
        _ensure_domain_blacklist_schema(con)
        _ensure_domain_perf_schema(con)
        _ensure_promotion_audit_columns(con)
        _ensure_promotion_watch_schema(con)
        _ensure_strategy_metrics_schema(con)
        _ensure_universe_audit_schema(con)
        _ensure_execution_mode_armed_column(con)
        _ensure_kill_switch_schema(con)
        _ensure_trade_attribution_ledger_schema(con)
        _ensure_shadow_capital_schema(con)

        # Additive: ensure symbols table exists even for older DBs
        try:
            con.execute("SELECT 1 FROM symbols LIMIT 1").fetchone()
        except Exception:
            try:
                con.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS symbols (
                      symbol TEXT PRIMARY KEY,
                      asset_class TEXT NOT NULL DEFAULT 'UNKNOWN',
                      status TEXT NOT NULL DEFAULT 'WATCH',
                      score REAL NOT NULL DEFAULT 0.0,
                      last_seen_event_ts_ms INTEGER,
                      last_traded_ts_ms INTEGER,
                      meta_json TEXT,
                      created_ts_ms INTEGER NOT NULL,
                      updated_ts_ms INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_symbols_status_score
                      ON symbols(status, score);
                    CREATE INDEX IF NOT EXISTS idx_symbols_updated
                      ON symbols(updated_ts_ms);
                    """
                )
            except Exception:
                pass
        try:
            con.commit()
        except Exception:
            pass

        try:
            _maybe_wal_checkpoint(con, force=True)
        except Exception:
            pass

    finally:
        try:
            con.close()
        except Exception:
            pass

def put_event(ts_ms, source, title, body, url, event_key, meta_json=None):

    con = connect(readonly=False)
    try:
        cur = con.cursor()

        cur.execute(
            """
            INSERT OR IGNORE INTO events
              (ts_ms, source, title, body, url, event_key, meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(ts_ms),
                str(source),
                str(title),
                body,
                url,
                str(event_key),
                meta_json,
            ),
        )

        row = cur.execute(
            "SELECT id FROM events WHERE event_key=?",
            (str(event_key),),
        ).fetchone()

        return int(row[0])
    finally:
        try:
            con.commit()
        except Exception:
            pass
        try:
            _note_write(con)
        except Exception:
            pass
        try:
            _maybe_wal_checkpoint(con, force=True)
        except Exception:
            pass
        try:
            con.close()
        except Exception:
            pass


def put_price(ts_ms, symbol, price):
    con = connect(readonly=False)
    try:

        con.execute(
            """
            INSERT INTO prices(ts_ms, symbol, price)

            VALUES (?, ?, ?)
            ON CONFLICT(symbol, ts_ms) DO UPDATE SET
              price=excluded.price
            """,
            (
                int(ts_ms),
                str(symbol),
                float(price),
            ),
        )
    finally:
        try:
            con.commit()
        except Exception:
            pass
        try:
            _note_write(con)
        except Exception:
            pass
        try:
            con.close()
        except Exception:
            pass


def acquire_job_lock(job_name: str, owner: str, pid: int, ttl_s: int = 180) -> bool:
    """
    Best-effort single-instance lock.
    Returns True if lock acquired/renewed, False otherwise.
    """
    import os
    import time

    # Enforce supervisor-only job starts by default.
    # Override ONLY when intentionally running a job manually:
    #   ALLOW_STANDALONE_JOBS=1 python <job>.py
    if os.environ.get("ENGINE_LAUNCHED_BY_SUPERVISOR", "0") != "1" and os.environ.get("ALLOW_STANDALONE_JOBS", "0") != "1":
        return False

    now_ms = int(time.time() * 1000)
    stale_ms = int(ttl_s) * 1000

    con = connect(readonly=False)

    try:
        con.execute("BEGIN IMMEDIATE;")
        row = con.execute(
            "SELECT owner, pid, heartbeat_ts_ms FROM job_locks WHERE job_name=?",
            (str(job_name),),
        ).fetchone()

        if row is None:
            con.execute(
                """
                INSERT INTO job_locks(job_name, owner, pid, acquired_ts_ms, heartbeat_ts_ms)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(job_name), str(owner), int(pid), now_ms, now_ms),
            )
            con.execute("COMMIT;")
            try:
                _note_write(con)
            except Exception:
                pass
            return True

        cur_owner, cur_pid, hb_ms = str(row[0]), int(row[1]), int(row[2])
        is_stale = (now_ms - hb_ms) > stale_ms
        is_same = (cur_owner == str(owner) and cur_pid == int(pid))

        if is_same or is_stale:
            con.execute(
                """
                UPDATE job_locks
                SET owner=?, pid=?, heartbeat_ts_ms=?
                WHERE job_name=?
                """,
                (str(owner), int(pid), now_ms, str(job_name)),
            )
            con.execute("COMMIT;")
            try:
                _note_write(con)
            except Exception:
                pass
            return True

        con.execute("ROLLBACK;")
        return False
    except Exception:
        try:
            con.execute("ROLLBACK;")
        except Exception:
            pass
        return False
    finally:
        try:
            con.close()
        except Exception:
            pass


def release_job_lock(job_name: str, owner: str, pid: int) -> None:
    con = connect(readonly=False)
    try:
        con.execute(
            "DELETE FROM job_locks WHERE job_name=? AND owner=? AND pid=?",
            (str(job_name), str(owner), int(pid)),
        )
        try:
            con.commit()
        except Exception:
            pass
        try:
            _note_write(con)
        except Exception:
            pass
    finally:
        try:
            con.close()
        except Exception:
            pass


def touch_job_lock(job_name: str, owner: str, pid: int) -> None:
    import time

    now_ms = int(time.time() * 1000)
    con = connect(readonly=False)
    try:
        con.execute(
            """
            UPDATE job_locks
            SET heartbeat_ts_ms=?
            WHERE job_name=? AND owner=? AND pid=?
            """,
            (now_ms, str(job_name), str(owner), int(pid)),
        )

    finally:
        try:
            con.commit()
        except Exception:
            pass
        try:
            _note_write(con)
        except Exception:
            pass
        try:
            con.close()
        except Exception:
            pass

def put_job_heartbeat(job_name: str, owner: str, pid: int, extra_json: str = None) -> None:
    import time

    now_ms = int(time.time() * 1000)
    con = connect(readonly=False)

    try:
        con.execute(
            """
            INSERT INTO job_heartbeats(job_name, owner, pid, ts_ms, extra_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(job_name) DO UPDATE SET
              owner=excluded.owner,
              pid=excluded.pid,
              ts_ms=excluded.ts_ms,
              extra_json=excluded.extra_json
            """,
            (str(job_name), str(owner), int(pid), now_ms, extra_json),
        )

    finally:
        try:
            con.commit()
        except Exception:
            pass
        try:
            _note_write(con)
        except Exception:
            pass
        try:
            con.close()
        except Exception:
            pass

def get_job_checkpoint(job_name: str) -> Dict[str, int]:
    con = connect_ro()
    try:
        row = con.execute(
            "SELECT last_event_id, last_event_ts_ms FROM job_checkpoints WHERE job_name=?",
            (str(job_name),),
        ).fetchone()
        if not row:
            return {"last_event_id": 0, "last_event_ts_ms": 0}
        return {
            "last_event_id": int(row[0] or 0),
            "last_event_ts_ms": int(row[1] or 0),
        }
    finally:
        try:
            con.close()
        except Exception:
            pass

def put_job_checkpoint(job_name: str, last_event_id: int, last_event_ts_ms: int) -> None:
    now_ms = int(time.time() * 1000)
    con = connect(readonly=False)
    try:
        con.execute(
            """
            INSERT INTO job_checkpoints(job_name, last_event_id, last_event_ts_ms, updated_ts_ms)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(job_name) DO UPDATE SET
              last_event_id=excluded.last_event_id,
              last_event_ts_ms=excluded.last_event_ts_ms,
              updated_ts_ms=excluded.updated_ts_ms
            """,
            (str(job_name), int(last_event_id), int(last_event_ts_ms), int(now_ms)),
        )

    finally:
        try:
            con.commit()
        except Exception:
            pass
        try:
            _note_write(con)
        except Exception:
            pass
        try:
            con.close()
        except Exception:
            pass

def close_pooled_connections() -> None:
    for key in ("rw", "ro"):
        con = getattr(_TLS, key, None)
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
            setattr(_TLS, key, None)
