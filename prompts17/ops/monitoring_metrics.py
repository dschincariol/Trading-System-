"""
Monitoring Metrics Collection System

Collects, stores, and aggregates metrics for SLO evaluation.
Provides time-series data for alerting and dashboard visualization.
"""

import time
import json
import sqlite3
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from contextlib import contextmanager
from engine.storage import connect

@dataclass
class MetricPoint:
    timestamp_ms: int
    metric_name: str
    value: float
    tags: Dict[str, str]
    source: str

@dataclass
class MetricAggregation:
    metric_name: str
    window_minutes: int
    aggregation_type: str  # avg, min, max, sum, count
    value: float
    sample_count: int
    window_start_ms: int
    window_end_ms: int

class MetricsCollector:
    """Collects and stores monitoring metrics"""
    
    def __init__(self):
        self._init_tables()
    
    def _init_tables(self):
        """Initialize metrics storage tables"""
        with connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS metrics_raw (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_ms INTEGER NOT NULL,
                    metric_name TEXT NOT NULL,
                    value REAL NOT NULL,
                    tags_json TEXT,
                    source TEXT,
                    created_at_ms INTEGER DEFAULT (strftime('%s', 'now') * 1000)
                )
            """)
            
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_metrics_name_time 
                ON metrics_raw(metric_name, timestamp_ms)
            """)
            
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_metrics_time 
                ON metrics_raw(timestamp_ms)
            """)
            
            # Aggregated metrics table for faster queries
            con.execute("""
                CREATE TABLE IF NOT EXISTS metrics_aggregated (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_name TEXT NOT NULL,
                    window_minutes INTEGER NOT NULL,
                    aggregation_type TEXT NOT NULL,
                    value REAL NOT NULL,
                    sample_count INTEGER NOT NULL,
                    window_start_ms INTEGER NOT NULL,
                    window_end_ms INTEGER NOT NULL,
                    created_at_ms INTEGER DEFAULT (strftime('%s', 'now') * 1000),
                    UNIQUE(metric_name, window_minutes, aggregation_type, window_start_ms)
                )
            """)
            
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_metrics_agg_name_time 
                ON metrics_aggregated(metric_name, window_start_ms, window_minutes)
            """)

    def record_metric(self, metric_name: str, value: float, 
                     tags: Optional[Dict[str, str]] = None,
                     source: str = "system") -> None:
        """Record a single metric point"""
        timestamp_ms = int(time.time() * 1000)
        tags_json = json.dumps(tags or {}, separators=(',', ':'))
        
        with connect() as con:
            con.execute("""
                INSERT INTO metrics_raw 
                (timestamp_ms, metric_name, value, tags_json, source)
                VALUES (?, ?, ?, ?, ?)
            """, (timestamp_ms, metric_name, value, tags_json, source))

    def record_metrics_batch(self, metrics: List[MetricPoint]) -> None:
        """Record multiple metrics efficiently"""
        if not metrics:
            return
            
        rows = []
        for m in metrics:
            tags_json = json.dumps(m.tags, separators=(',', ':'))
            rows.append((
                m.timestamp_ms, m.metric_name, m.value, 
                tags_json, m.source
            ))
        
        with connect() as con:
            con.executemany("""
                INSERT INTO metrics_raw 
                (timestamp_ms, metric_name, value, tags_json, source)
                VALUES (?, ?, ?, ?, ?)
            """, rows)

    def get_recent_metrics(self, metric_name: str, minutes_back: int = 60,
                          tags_filter: Optional[Dict[str, str]] = None) -> List[MetricPoint]:
        """Get recent metrics for a given name"""
        cutoff_ms = int((time.time() - minutes_back * 60) * 1000)
        
        query = """
            SELECT timestamp_ms, metric_name, value, tags_json, source
            FROM metrics_raw 
            WHERE metric_name = ? AND timestamp_ms >= ?
            ORDER BY timestamp_ms DESC
        """
        params = [metric_name, cutoff_ms]
        
        if tags_filter:
            # Simple tag filtering - in production would use proper tag index
            for key, val in tags_filter.items():
                query += f" AND tags_json LIKE ?"
                params.append(f'%"{key}":"{val}"%')
        
        with connect() as con:
            rows = con.execute(query, params).fetchall()
            
            metrics = []
            for row in rows:
                tags = json.loads(row[3] or '{}')
                metrics.append(MetricPoint(
                    timestamp_ms=row[0],
                    metric_name=row[1],
                    value=row[2],
                    tags=tags,
                    source=row[4]
                ))
            
            return metrics

    def aggregate_metrics(self, metric_name: str, window_minutes: int,
                         aggregation_type: str = "avg",
                         minutes_back: int = 1440) -> List[MetricAggregation]:
        """Aggregate metrics over time windows"""
        cutoff_ms = int((time.time() - minutes_back * 60) * 1000)
        window_ms = window_minutes * 60 * 1000
        
        agg_func = {
            "avg": "AVG(value)",
            "min": "MIN(value)", 
            "max": "MAX(value)",
            "sum": "SUM(value)",
            "count": "COUNT(value)"
        }.get(aggregation_type, "AVG(value)")
        
        with connect() as con:
            rows = con.execute(f"""
                SELECT 
                    metric_name,
                    ? as window_minutes,
                    ? as aggregation_type,
                    {agg_func} as value,
                    COUNT(*) as sample_count,
                    (timestamp_ms / ?) * ? as window_start_ms,
                    ((timestamp_ms / ?) + 1) * ? - 1 as window_end_ms
                FROM metrics_raw 
                WHERE metric_name = ? AND timestamp_ms >= ?
                GROUP BY timestamp_ms / ?
                ORDER BY window_start_ms DESC
            """, [
                window_minutes, aggregation_type,
                window_ms, window_ms, window_ms, window_ms,
                metric_name, cutoff_ms, window_ms
            ]).fetchall()
            
            aggregations = []
            for row in rows:
                aggregations.append(MetricAggregation(
                    metric_name=row[0],
                    window_minutes=row[1],
                    aggregation_type=row[2],
                    value=row[3],
                    sample_count=row[4],
                    window_start_ms=row[5],
                    window_end_ms=row[6]
                ))
            
            return aggregations

    def get_latest_value(self, metric_name: str,
                        tags_filter: Optional[Dict[str, str]] = None) -> Optional[float]:
        """Get the most recent value for a metric"""
        metrics = self.get_recent_metrics(metric_name, minutes_back=1, tags_filter=tags_filter)
        return metrics[0].value if metrics else None

class TradingMetricsCollector(MetricsCollector):
    """Specialized metrics collector for trading system"""
    
    def record_data_freshness(self, data_type: str, age_seconds: float):
        """Record data freshness metrics"""
        self.record_metric(f"data_freshness.{data_type}", age_seconds, 
                          {"data_type": data_type}, "data_pipeline")
    
    def record_model_health(self, metric_name: str, value: float, model_id: str = "default"):
        """Record model health metrics"""
        self.record_metric(f"model.{metric_name}", value,
                          {"model_id": model_id}, "model_monitor")
    
    def record_execution_quality(self, metric_name: str, value: float, 
                                symbol: Optional[str] = None):
        """Record execution quality metrics"""
        tags = {}
        if symbol:
            tags["symbol"] = symbol
        self.record_metric(f"execution.{metric_name}", value, tags, "execution")
    
    def record_job_reliability(self, job_name: str, metric_name: str, value: float):
        """Record job reliability metrics"""
        self.record_metric(f"job.{job_name}.{metric_name}", value,
                          {"job_name": job_name}, "job_monitor")
    
    def record_system_health(self, metric_name: str, value: float):
        """Record system-wide health metrics"""
        self.record_metric(f"system.{metric_name}", value, {}, "system_monitor")

# Global metrics collector instance
metrics_collector = TradingMetricsCollector()
