# engine/execution/execution_ai_features.py
"""
Execution Microstructure AI Feature Engineering

Extracts predictive features from execution data for slippage modeling
and optimal execution decision making.
"""

import json
import math
import time
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass

from engine.storage import connect


@dataclass
class ExecutionFeatures:
    """Feature set for execution AI decision making"""
    
    # Market microstructure features
    volatility: float
    spread_bps: float
    depth_imbalance: float
    mid_price_mvmt: float
    
    # Order-specific features  
    order_size: float
    size_participation: float
    side: int  # 1 for buy, -1 for sell
    aggressiveness: str
    
    # Temporal features
    time_of_day: float  # 0-1 normalized
    day_of_week: int    # 0-6
    market_session: str # pre/market/post/close
    
    # Liquidity features
    maker_taker_ratio: float
    fill_rate_history: float
    recent_slippage: float
    
    # Alpha decay features
    alpha_ttl_ms: int
    alpha_half_life_ms: int
    urgency_score: float
    
    # Broker-specific features
    broker: str
    venue: str
    instrument_type: str
    
    # Historical performance
    hist_slippage_bps: float
    hist_fill_latency_ms: float
    hist_cost_efficiency: float


def _ensure_feature_tables(con) -> None:
    """Create tables for feature storage and model training"""
    con.executescript("""
    CREATE TABLE IF NOT EXISTS execution_features (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_ms INTEGER NOT NULL,
        client_order_id TEXT NOT NULL,
        broker TEXT,
        symbol TEXT NOT NULL,
        
        -- Market features
        volatility REAL,
        spread_bps REAL,
        depth_imbalance REAL,
        mid_price_mvmt REAL,
        
        -- Order features
        order_size REAL,
        size_participation REAL,
        side INTEGER,
        aggressiveness TEXT,
        
        -- Temporal features
        time_of_day REAL,
        day_of_week INTEGER,
        market_session TEXT,
        
        -- Liquidity features
        maker_taker_ratio REAL,
        fill_rate_history REAL,
        recent_slippage REAL,
        
        -- Alpha features
        alpha_ttl_ms INTEGER,
        alpha_half_life_ms INTEGER,
        urgency_score REAL,
        
        -- Context features
        venue TEXT,
        instrument_type TEXT,
        
        -- Historical performance
        hist_slippage_bps REAL,
        hist_fill_latency_ms REAL,
        hist_cost_efficiency REAL,
        
        -- Labels (for training)
        actual_slippage_bps REAL,
        actual_fill_latency_ms REAL,
        actual_cost_bps REAL,
        
        created_ts_ms INTEGER,
        features_json TEXT,
        
        UNIQUE(client_order_id)
    );
    
    CREATE INDEX IF NOT EXISTS idx_exec_features_ts ON execution_features(ts_ms);
    CREATE INDEX IF NOT EXISTS idx_exec_features_symbol ON execution_features(symbol);
    CREATE INDEX IF NOT EXISTS idx_exec_features_broker ON execution_features(broker);
    
    -- Model training data
    CREATE TABLE IF NOT EXISTS execution_model_training (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        model_version TEXT NOT NULL,
        training_ts_ms INTEGER NOT NULL,
        feature_columns TEXT NOT NULL,
        target_column TEXT NOT NULL,
        sample_count INTEGER NOT NULL,
        train_score REAL,
        val_score REAL,
        model_params_json TEXT,
        feature_importance_json TEXT,
        created_ts_ms INTEGER
    );
    
    CREATE INDEX IF NOT EXISTS idx_exec_model_training_version ON execution_model_training(model_version);
    """)


def _get_market_features(con, symbol: str, ts_ms: int) -> Dict[str, float]:
    """Extract real-time market microstructure features"""
    
    # Get recent price history for volatility calculation
    price_rows = con.execute("""
        SELECT px, ts_ms 
        FROM prices 
        WHERE symbol = ? AND ts_ms BETWEEN ? AND ?
        ORDER BY ts_ms DESC
        LIMIT 100
    """, (symbol, ts_ms - 300_000, ts_ms)).fetchall()  # 5 min window
    
    volatility = 0.0
    mid_price_mvmt = 0.0
    
    if len(price_rows) >= 10:
        prices = [float(row[0]) for row in price_rows if row[0] is not None]
        if len(prices) >= 10:
            returns = []
            for i in range(1, len(prices)):
                if prices[i-1] > 0:
                    ret = (prices[i] - prices[i-1]) / prices[i-1]
                    returns.append(ret)
            
            if returns:
                volatility = math.sqrt(sum(r*r for r in returns) / len(returns)) * math.sqrt(252) * 10000  # annualized bps
                
                # Mid price movement (last 30 seconds vs prior)
                if len(prices) >= 60:
                    recent_avg = sum(prices[:30]) / 30
                    prior_avg = sum(prices[30:60]) / 30
                    if prior_avg > 0:
                        mid_price_mvmt = ((recent_avg - prior_avg) / prior_avg) * 10000
    
    # Get spread from order book if available, otherwise use recent trades
    spread_bps = 2.5  # default fallback
    
    # Try to get spread from recent execution data
    spread_row = con.execute("""
        SELECT AVG(spread_bps)
        FROM execution_analytics
        WHERE symbol = ? AND ts_ms BETWEEN ? AND ?
        LIMIT 20
    """, (symbol, ts_ms - 60_000, ts_ms)).fetchone()
    
    if spread_row and spread_row[0] is not None:
        spread_bps = float(spread_row[0])
    
    # Depth imbalance (proxy using trade direction imbalance)
    depth_imbalance = 0.0
    trade_rows = con.execute("""
        SELECT fill_qty, side
        FROM execution_fills f
        JOIN execution_orders o ON f.client_order_id = o.client_order_id
        WHERE f.symbol = ? AND f.fill_ts_ms BETWEEN ? AND ?
        LIMIT 50
    """, (symbol, ts_ms - 60_000, ts_ms)).fetchall()
    
    if trade_rows:
        buy_volume = sum(qty for qty, side in trade_rows if side and side.upper().startswith('B'))
        sell_volume = sum(qty for qty, side in trade_rows if side and side.upper().startswith('S'))
        total_volume = buy_volume + sell_volume
        if total_volume > 0:
            depth_imbalance = (buy_volume - sell_volume) / total_volume
    
    return {
        'volatility': volatility,
        'spread_bps': spread_bps,
        'depth_imbalance': depth_imbalance,
        'mid_price_mvmt': mid_price_mvmt
    }


def _get_historical_performance(con, broker: str, symbol: str, aggressiveness: str) -> Dict[str, float]:
    """Get historical execution performance for similar orders"""
    
    perf_row = con.execute("""
        SELECT 
            AVG(slippage_bps) as avg_slip,
            AVG(age_ms) as avg_latency,
            AVG(total_cost_bps) as avg_cost,
            COUNT(*) as sample_count
        FROM execution_analytics
        WHERE broker = ? 
          AND symbol = ? 
          AND aggressiveness = ?
          AND ts_ms > ?
        LIMIT 100
    """, (broker, symbol, aggressiveness, time.time() * 1000 - 7*24*3600*1000)).fetchone()
    
    if perf_row and perf_row[3] and perf_row[3] >= 10:  # need at least 10 samples
        return {
            'hist_slippage_bps': float(perf_row[0] or 0),
            'hist_fill_latency_ms': float(perf_row[1] or 0),
            'hist_cost_efficiency': 100.0 / max(1.0, float(perf_row[2] or 1.0))
        }
    
    # Fallback to broker-level stats
    broker_row = con.execute("""
        SELECT 
            AVG(slippage_bps) as avg_slip,
            AVG(age_ms) as avg_latency,
            COUNT(*) as sample_count
        FROM execution_analytics
        WHERE broker = ? AND ts_ms > ?
        LIMIT 100
    """, (broker, time.time() * 1000 - 7*24*3600*1000)).fetchone()
    
    if broker_row and broker_row[2] and broker_row[2] >= 10:
        return {
            'hist_slippage_bps': float(broker_row[0] or 0),
            'hist_fill_latency_ms': float(broker_row[1] or 0),
            'hist_cost_efficiency': 100.0 / max(1.0, float(broker_row[0] or 1.0))
        }
    
    # Default values
    return {
        'hist_slippage_bps': 2.5,
        'hist_fill_latency_ms': 500.0,
        'hist_cost_efficiency': 50.0
    }


def _get_temporal_features(ts_ms: int) -> Dict[str, Any]:
    """Extract time-based features"""
    
    dt = time.localtime(ts_ms / 1000)
    
    # Time of day (0-1 normalized, market hours weighted)
    hour = dt.tm_hour + dt.tm_min / 60.0
    if 9.5 <= hour <= 16.0:  # market hours
        time_of_day = (hour - 9.5) / 6.5
    else:
        time_of_day = 0.5  # off-hours default
    
    # Market session
    if hour < 9.5:
        market_session = 'pre'
    elif hour <= 16.0:
        market_session = 'market'
    else:
        market_session = 'post'
    
    return {
        'time_of_day': time_of_day,
        'day_of_week': dt.tm_wday,
        'market_session': market_session
    }


def _calculate_urgency_score(alpha_ttl_ms: int, alpha_half_life_ms: int, age_ms: int) -> float:
    """Calculate urgency score based on alpha decay parameters"""
    
    if alpha_ttl_ms <= 0:
        return 0.5  # medium urgency if no TTL
    
    # Time decay factor
    time_factor = min(1.0, age_ms / max(1, alpha_ttl_ms))
    
    # Half-life urgency
    if alpha_half_life_ms > 0:
        decay_factor = math.pow(0.5, age_ms / alpha_half_life_ms)
        urgency = 1.0 - decay_factor
    else:
        urgency = time_factor
    
    return max(0.0, min(1.0, urgency))


def extract_execution_features(
    client_order_id: str,
    broker: str,
    symbol: str,
    qty: float,
    aggressiveness: str,
    alpha_ttl_ms: int = 0,
    alpha_half_life_ms: int = 60000,
    venue: Optional[str] = None,
    instrument_type: Optional[str] = None,
    submit_ts_ms: Optional[int] = None
) -> ExecutionFeatures:
    """Extract comprehensive execution features for AI decision making"""
    
    if submit_ts_ms is None:
        submit_ts_ms = int(time.time() * 1000)
    
    con = connect()
    try:
        _ensure_feature_tables(con)
        
        # Get market features
        market_features = _get_market_features(con, symbol, submit_ts_ms)
        
        # Get historical performance
        hist_perf = _get_historical_performance(con, broker, symbol, aggressiveness)
        
        # Get temporal features
        temporal_features = _get_temporal_features(submit_ts_ms)
        
        # Calculate urgency
        age_ms = 0  # new order
        urgency_score = _calculate_urgency_score(alpha_ttl_ms, alpha_half_life_ms, age_ms)
        
        # Size participation (relative to average daily volume)
        size_participation = 0.01  # default 1%
        try:
            adv_row = con.execute("""
                SELECT AVG(volume) FROM daily_volume 
                WHERE symbol = ? AND date >= date('now', '-30 days')
                LIMIT 1
            """, (symbol,)).fetchone()
            
            if adv_row and adv_row[0]:
                adv = float(adv_row[0])
                if adv > 0:
                    size_participation = abs(qty) / adv
        except Exception:
            pass
        
        # Liquidity features (maker/taker ratio from recent fills)
        maker_taker_ratio = 0.5  # default
        try:
            liq_row = con.execute("""
                SELECT 
                    SUM(CASE WHEN liquidity = 'M' THEN 1 ELSE 0 END) / COUNT(*) as maker_ratio
                FROM execution_fills f
                JOIN execution_orders o ON f.client_order_id = o.client_order_id
                WHERE o.symbol = ? AND f.fill_ts_ms > ?
            """, (symbol, time.time() * 1000 - 24*3600*1000)).fetchone()
            
            if liq_row and liq_row[0]:
                maker_taker_ratio = float(liq_row[0])
        except Exception:
            pass
        
        # Fill rate history
        fill_rate_history = 0.95  # default
        try:
            fill_row = con.execute("""
                SELECT 
                    COUNT(CASE WHEN status = 'filled' THEN 1 END) / COUNT(*) as fill_rate
                FROM execution_orders
                WHERE symbol = ? AND broker = ? AND submit_ts_ms > ?
            """, (symbol, broker, time.time() * 1000 - 7*24*3600*1000)).fetchone()
            
            if fill_row and fill_row[0]:
                fill_rate_history = float(fill_row[0])
        except Exception:
            pass
        
        # Recent slippage trend
        recent_slippage = 0.0
        try:
            slip_row = con.execute("""
                SELECT AVG(slippage_bps)
                FROM execution_analytics
                WHERE symbol = ? AND broker = ? AND ts_ms > ?
                ORDER BY ts_ms DESC
                LIMIT 20
            """, (symbol, broker, time.time() * 1000 - 24*3600*1000)).fetchone()
            
            if slip_row and slip_row[0]:
                recent_slippage = float(slip_row[0])
        except Exception:
            pass
        
        return ExecutionFeatures(
            volatility=market_features['volatility'],
            spread_bps=market_features['spread_bps'],
            depth_imbalance=market_features['depth_imbalance'],
            mid_price_mvmt=market_features['mid_price_mvmt'],
            
            order_size=abs(qty),
            size_participation=size_participation,
            side=1 if qty > 0 else -1,
            aggressiveness=aggressiveness,
            
            time_of_day=temporal_features['time_of_day'],
            day_of_week=temporal_features['day_of_week'],
            market_session=temporal_features['market_session'],
            
            maker_taker_ratio=maker_taker_ratio,
            fill_rate_history=fill_rate_history,
            recent_slippage=recent_slippage,
            
            alpha_ttl_ms=alpha_ttl_ms,
            alpha_half_life_ms=alpha_half_life_ms,
            urgency_score=urgency_score,
            
            broker=broker,
            venue=venue or 'UNKNOWN',
            instrument_type=instrument_type or 'EQUITY',
            
            hist_slippage_bps=hist_perf['hist_slippage_bps'],
            hist_fill_latency_ms=hist_perf['hist_fill_latency_ms'],
            hist_cost_efficiency=hist_perf['hist_cost_efficiency']
        )
        
    finally:
        con.close()


def store_execution_features(features: ExecutionFeatures, client_order_id: str) -> None:
    """Store extracted features for model training"""
    
    con = connect()
    try:
        _ensure_feature_tables(con)
        
        feature_dict = {
            'volatility': features.volatility,
            'spread_bps': features.spread_bps,
            'depth_imbalance': features.depth_imbalance,
            'mid_price_mvmt': features.mid_price_mvmt,
            'order_size': features.order_size,
            'size_participation': features.size_participation,
            'side': features.side,
            'aggressiveness': features.aggressiveness,
            'time_of_day': features.time_of_day,
            'day_of_week': features.day_of_week,
            'market_session': features.market_session,
            'maker_taker_ratio': features.maker_taker_ratio,
            'fill_rate_history': features.fill_rate_history,
            'recent_slippage': features.recent_slippage,
            'alpha_ttl_ms': features.alpha_ttl_ms,
            'alpha_half_life_ms': features.alpha_half_life_ms,
            'urgency_score': features.urgency_score,
            'venue': features.venue,
            'instrument_type': features.instrument_type,
            'hist_slippage_bps': features.hist_slippage_bps,
            'hist_fill_latency_ms': features.hist_fill_latency_ms,
            'hist_cost_efficiency': features.hist_cost_efficiency
        }
        
        con.execute("""
            INSERT OR REPLACE INTO execution_features(
                ts_ms, client_order_id, broker, symbol,
                volatility, spread_bps, depth_imbalance, mid_price_mvmt,
                order_size, size_participation, side, aggressiveness,
                time_of_day, day_of_week, market_session,
                maker_taker_ratio, fill_rate_history, recent_slippage,
                alpha_ttl_ms, alpha_half_life_ms, urgency_score,
                venue, instrument_type,
                hist_slippage_bps, hist_fill_latency_ms, hist_cost_efficiency,
                created_ts_ms, features_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            int(time.time() * 1000),
            client_order_id,
            features.broker,
            '',  # symbol will be updated when known
            feature_dict['volatility'],
            feature_dict['spread_bps'],
            feature_dict['depth_imbalance'],
            feature_dict['mid_price_mvmt'],
            feature_dict['order_size'],
            feature_dict['size_participation'],
            feature_dict['side'],
            feature_dict['aggressiveness'],
            feature_dict['time_of_day'],
            feature_dict['day_of_week'],
            feature_dict['market_session'],
            feature_dict['maker_taker_ratio'],
            feature_dict['fill_rate_history'],
            feature_dict['recent_slippage'],
            feature_dict['alpha_ttl_ms'],
            feature_dict['alpha_half_life_ms'],
            feature_dict['urgency_score'],
            feature_dict['venue'],
            feature_dict['instrument_type'],
            feature_dict['hist_slippage_bps'],
            feature_dict['hist_fill_latency_ms'],
            feature_dict['hist_cost_efficiency'],
            int(time.time() * 1000),
            json.dumps(feature_dict, separators=(',', ':'), sort_keys=True)
        ))
        
        con.commit()
        
    finally:
        con.close()


def get_training_data(
    lookback_days: int = 30,
    min_samples: int = 100
) -> Tuple[List[Dict[str, float]], List[float]]:
    """Get training data for execution AI model"""
    
    con = connect()
    try:
        _ensure_feature_tables(con)
        
        since_ts = int(time.time() * 1000) - (lookback_days * 24 * 3600 * 1000)
        
        rows = con.execute("""
            SELECT features_json, actual_slippage_bps
            FROM execution_features
            WHERE ts_ms >= ? 
              AND actual_slippage_bps IS NOT NULL
            ORDER BY ts_ms DESC
            LIMIT ?
        """, (since_ts, min_samples * 2)).fetchall()
        
        if len(rows) < min_samples:
            return [], []
        
        X = []
        y = []
        
        for features_json, slippage_bps in rows:
            try:
                features = json.loads(features_json or '{}')
                if isinstance(features, dict):
                    X.append(features)
                    y.append(float(slippage_bps))
            except Exception:
                continue
        
        return X, y
        
    finally:
        con.close()
