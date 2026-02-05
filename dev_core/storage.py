# dev_core/storage.py
import os
import sqlite3
from pathlib import Path

DB_PATH = Path("dev.db")

# Production-stable WAL defaults + performance tuning (env-controlled)
#
# Tune via env:
#   SQLITE_CACHE_KB=-2000000         (~2GB page cache)
#   SQLITE_MMAP_BYTES=30000000000    (~30GB mmap)
#   SQLITE_WAL_AUTOCHECKPOINT=2000
#   SQLITE_JOURNAL_SIZE_LIMIT=268435456
#
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


def connect():
    con = sqlite3.connect(
        DB_PATH,
        timeout=30.0,
        isolation_level=None,  # autocommit
        check_same_thread=False,
    )
    for p in _SQLITE_PRAGMAS:
        try:
            con.execute(p)
        except Exception:
            pass
    return con


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


def init_db():
    con = connect()
    try:
        con.executescript(
            """
            -- -------------------------------------------------------
            -- Dynamic symbol universe (WATCH → ACTIVE)
            -- -------------------------------------------------------
            CREATE TABLE IF NOT EXISTS symbol_universe (
              symbol TEXT PRIMARY KEY,
              status TEXT NOT NULL,            -- WATCH | ACTIVE | BLOCKED
              first_seen_ms INTEGER NOT NULL,
              last_seen_ms INTEGER NOT NULL,
              last_promoted_ms INTEGER,
              seen_n INTEGER NOT NULL DEFAULT 1,
              meta_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_symbol_universe_status
              ON symbol_universe(status);

            -- -------------------------------------------------------
            -- Core ingestion
            -- -------------------------------------------------------
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

            CREATE TABLE IF NOT EXISTS prices (
              ts_ms INTEGER NOT NULL,
              symbol TEXT NOT NULL,
              price REAL NOT NULL,
              PRIMARY KEY(symbol, ts_ms)
            );

            CREATE INDEX IF NOT EXISTS idx_prices_symbol_ts
              ON prices(symbol, ts_ms);

            -- -------------------------------------------------------
            -- Optional: computed market/tech features (versioned JSON)
            -- -------------------------------------------------------
            CREATE TABLE IF NOT EXISTS market_features (
              ts_ms INTEGER NOT NULL,
              symbol TEXT NOT NULL,
              v INTEGER NOT NULL,
              features_json TEXT NOT NULL,
              PRIMARY KEY(symbol, ts_ms, v)
            );

            CREATE INDEX IF NOT EXISTS idx_market_features_symbol_ts
              ON market_features(symbol, ts_ms);

                          -- -------------------------------------------------------
            -- Quote snapshots (bid/ask/spread/volume)
            -- -------------------------------------------------------
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

            -- -------------------------------------------------------
            -- Provider health (for auto-failover)
            -- -------------------------------------------------------
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

            -- -------------------------------------------------------
            -- Ingest-side slippage proxy (mid vs last) per provider
            -- -------------------------------------------------------
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

            -- -------------------------------------------------------
            -- Dynamic symbol universe (WATCH → ACTIVE → BLOCKED)
            -- -------------------------------------------------------
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

            -- -------------------------------------------------------
            -- OHLCV bars (for tradability + correlation + risk)
            -- tf_s: timeframe in seconds (e.g. 60 for 1m)
            -- -------------------------------------------------------
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

            -- -------------------------------------------------------
            -- Symbol registry (dynamic universe)
            -- -------------------------------------------------------
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

            -- -------------------------------------------------------
            -- Labels
            -- -------------------------------------------------------
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

            -- -------------------------------------------------------
            -- Embeddings
            -- -------------------------------------------------------
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

            -- -------------------------------------------------------
            -- Equity / risk state
            -- -------------------------------------------------------
            CREATE TABLE IF NOT EXISTS equity_history (
              ts_ms INTEGER PRIMARY KEY,
              equity REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS risk_state (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_ts_ms INTEGER NOT NULL
            );

            -- -------------------------------------------------------
            -- Job locks + heartbeats (production stability)
            -- -------------------------------------------------------
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

            -- -------------------------------------------------------
            -- Temporal models
            -- -------------------------------------------------------
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

            -- -------------------------------------------------------
            -- Drift & backtests
            -- -------------------------------------------------------
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

            -- -------------------------------------------------------
            -- Model registry (single canonical definition)
            -- -------------------------------------------------------
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


            -- -------------------------------------------------------
            -- Execution-aware labels (FIXED)
            -- -------------------------------------------------------
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

            -- -------------------------------------------------------
            -- Kill switches (system-wide, fail-closed)
            -- -------------------------------------------------------
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

            -- -------------------------------------------------------
            -- Shadow training runs
            -- -------------------------------------------------------
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

            -- -------------------------------------------------------
            -- Shadow metrics (non-executing)
            -- -------------------------------------------------------
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

            -- -------------------------------------------------------
            -- Decision log
            -- -------------------------------------------------------
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

            -- -------------------------------------------------------
            -- Size policy (confidence -> sizing factor)
            -- -------------------------------------------------------
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

            -- -------------------------------------------------------
            -- Portfolio backtest outputs (required by dashboard preflight)
            -- -------------------------------------------------------
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

            """
        )

        _ensure_labels_columns(con)
        _ensure_events_columns(con)
        _ensure_price_quotes_schema(con)
        _ensure_options_chain_schema(con)
        _ensure_earnings_calendar_schema(con)
        _ensure_sec_filings_schema(con)
        _ensure_domain_blacklist_schema(con)
        _ensure_domain_perf_schema(con)
        _ensure_promotion_audit_columns(con)
        _ensure_promotion_watch_schema(con)
        _ensure_kill_switch_schema(con)

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

    finally:
        con.close()

def put_event(ts_ms, source, title, body, url, event_key, meta_json=None):

    con = connect()
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
        con.close()

def put_price(ts_ms, symbol, price):
    con = connect()
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
        con.close()

def acquire_job_lock(job_name: str, owner: str, pid: int, stale_after_s: int = 180) -> bool:
    """
    Best-effort single-instance lock.
    Returns True if lock acquired/renewed, False otherwise.
    """
    import time

    now_ms = int(time.time() * 1000)
    stale_ms = int(stale_after_s) * 1000

    con = connect()
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
        con.close()


def release_job_lock(job_name: str, owner: str, pid: int) -> None:
    con = connect()
    try:
        con.execute(
            "DELETE FROM job_locks WHERE job_name=? AND owner=? AND pid=?",
            (str(job_name), str(owner), int(pid)),
        )
    finally:
        con.close()


def touch_job_lock(job_name: str, owner: str, pid: int) -> None:
    import time

    now_ms = int(time.time() * 1000)
    con = connect()
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
        con.close()


def put_job_heartbeat(job_name: str, owner: str, pid: int, extra_json: str = None) -> None:
    import time

    now_ms = int(time.time() * 1000)
    con = connect()
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
        con.close()
