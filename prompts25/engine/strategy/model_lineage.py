"""
Model Lineage Tracking System
Tracks complete model lifecycle including versions, training data, promotions, and demotions.
Provides immutable historical records and audit-ready explanations.
"""

import json
import time
import logging
from typing import Dict, Any, List, Tuple, Optional
from dataclasses import dataclass, asdict
from enum import Enum
from datetime import datetime, timezone

from engine.storage import connect, init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class LineageEventType(Enum):
    MODEL_CREATED = "model_created"
    TRAINING_COMPLETED = "training_completed"
    DATA_VERSIONED = "data_versioned"
    PROMOTION_REQUESTED = "promotion_requested"
    PROMOTION_APPROVED = "promotion_approved"
    PROMOTION_REJECTED = "promotion_rejected"
    MODEL_PROMOTED = "model_promoted"
    MODEL_DEMOTED = "model_demoted"
    ROLLBACK_EXECUTED = "rollback_executed"
    QUARANTINE_PLACED = "quarantine_placed"
    QUARANTINE_LIFTED = "quarantine_lifted"
    MODEL_RETIRED = "model_retired"
    PERFORMANCE_DEGRADED = "performance_degraded"
    RISK_BREACH = "risk_breach"
    MANUAL_INTERVENTION = "manual_intervention"

class ModelStage(Enum):
    CANDIDATE = "candidate"
    CHALLENGER = "challenger"
    SHADOW = "shadow"
    CHAMPION = "champion"
    RETIRED = "retired"
    QUARANTINED = "quarantined"

@dataclass
class TrainingDataInfo:
    """Training data lineage information"""
    data_version: str
    data_source: str
    data_hash: str
    sample_count: int
    feature_count: int
    training_start_ts_ms: int
    training_end_ts_ms: int
    preprocessing_config: Dict[str, Any]
    data_quality_metrics: Dict[str, Any]

@dataclass
class ModelLineageEvent:
    """Immutable model lineage event"""
    event_id: str
    ts_ms: int
    event_type: LineageEventType
    model_name: str
    model_kind: str
    model_ts_ms: int
    regime: str
    actor: str
    reason: str
    metadata: Dict[str, Any]
    previous_state: Optional[Dict[str, Any]] = None
    new_state: Optional[Dict[str, Any]] = None

@dataclass
class ModelLineage:
    """Complete model lineage information"""
    model_name: str
    model_kind: str
    model_ts_ms: int
    regime: str
    created_ts_ms: int
    current_stage: ModelStage
    training_data: TrainingDataInfo
    events: List[ModelLineageEvent]
    parent_models: List[Dict[str, Any]]  # Models this was derived from
    child_models: List[Dict[str, Any]]   # Models derived from this
    metrics_history: List[Dict[str, Any]]
    promotion_chain: List[Dict[str, Any]]  # Chain of promotions/demotions

class ModelLineageTracker:
    """Comprehensive model lineage tracking system"""
    
    def __init__(self):
        self._init_lineage_tables()
    
    def _init_lineage_tables(self):
        """Initialize lineage tracking database tables"""
        init_db()
        con = connect()
        try:
            con.executescript("""
                -- Core lineage events table (immutable)
                CREATE TABLE IF NOT EXISTS model_lineage_events (
                    event_id TEXT PRIMARY KEY,
                    ts_ms INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    model_kind TEXT NOT NULL,
                    model_ts_ms INTEGER NOT NULL,
                    regime TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    previous_state_json TEXT,
                    new_state_json TEXT,
                    created_ts_ms INTEGER NOT NULL
                );
                
                -- Training data lineage
                CREATE TABLE IF NOT EXISTS training_data_lineage (
                    model_name TEXT NOT NULL,
                    model_kind TEXT NOT NULL,
                    model_ts_ms INTEGER NOT NULL,
                    regime TEXT NOT NULL,
                    data_version TEXT NOT NULL,
                    data_source TEXT NOT NULL,
                    data_hash TEXT NOT NULL,
                    sample_count INTEGER NOT NULL,
                    feature_count INTEGER NOT NULL,
                    training_start_ts_ms INTEGER NOT NULL,
                    training_end_ts_ms INTEGER NOT NULL,
                    preprocessing_config_json TEXT,
                    data_quality_metrics_json TEXT,
                    PRIMARY KEY(model_name, model_kind, model_ts_ms, regime)
                );
                
                -- Model relationships (parent-child)
                CREATE TABLE IF NOT EXISTS model_relationships (
                    parent_model_name TEXT NOT NULL,
                    parent_model_kind TEXT NOT NULL,
                    parent_model_ts_ms INTEGER NOT NULL,
                    child_model_name TEXT NOT NULL,
                    child_model_kind TEXT NOT NULL,
                    child_model_ts_ms INTEGER NOT NULL,
                    relationship_type TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    created_ts_ms INTEGER NOT NULL,
                    PRIMARY KEY(parent_model_name, parent_model_kind, parent_model_ts_ms,
                              child_model_name, child_model_kind, child_model_ts_ms)
                );
                
                -- Promotion chain tracking
                CREATE TABLE IF NOT EXISTS promotion_chain (
                    chain_id TEXT PRIMARY KEY,
                    model_name TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    created_ts_ms INTEGER NOT NULL,
                    chain_events_json TEXT NOT NULL
                );
                
                -- Model snapshots (state at key points)
                CREATE TABLE IF NOT EXISTS model_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    model_name TEXT NOT NULL,
                    model_kind TEXT NOT NULL,
                    model_ts_ms INTEGER NOT NULL,
                    regime TEXT NOT NULL,
                    snapshot_type TEXT NOT NULL,
                    snapshot_ts_ms INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    metrics_json TEXT,
                    created_ts_ms INTEGER NOT NULL
                );
                
                -- Indexes for efficient querying
                CREATE INDEX IF NOT EXISTS idx_lineage_events_model_time 
                    ON model_lineage_events(model_name, model_kind, model_ts_ms, ts_ms);
                CREATE INDEX IF NOT EXISTS idx_lineage_events_time 
                    ON model_lineage_events(ts_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_lineage_events_type 
                    ON model_lineage_events(event_type, ts_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_training_data_model 
                    ON training_data_lineage(model_name, model_kind, model_ts_ms);
                CREATE INDEX IF NOT EXISTS idx_model_relationships_parent 
                    ON model_relationships(parent_model_name, parent_model_kind, parent_model_ts_ms);
                CREATE INDEX IF NOT EXISTS idx_model_relationships_child 
                    ON model_relationships(child_model_name, child_model_kind, child_model_ts_ms);
                CREATE INDEX IF NOT EXISTS idx_promotion_chain_model 
                    ON promotion_chain(model_name, regime, created_ts_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_model_snapshots_model 
                    ON model_snapshots(model_name, model_kind, model_ts_ms, snapshot_ts_ms DESC);
            """)
            con.commit()
        finally:
            con.close()
    
    def record_event(
        self,
        event_type: LineageEventType,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        regime: str,
        actor: str,
        reason: str,
        metadata: Dict[str, Any],
        previous_state: Optional[Dict[str, Any]] = None,
        new_state: Optional[Dict[str, Any]] = None
    ) -> str:
        """Record an immutable lineage event"""
        event_id = f"{model_name}_{model_kind}_{model_ts_ms}_{int(time.time() * 1000)}_{event_type.value}"
        
        con = connect()
        try:
            con.execute(
                """
                INSERT INTO model_lineage_events
                (event_id, ts_ms, event_type, model_name, model_kind, model_ts_ms,
                 regime, actor, reason, metadata_json, previous_state_json, new_state_json, created_ts_ms)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_id,
                    int(time.time() * 1000),
                    event_type.value,
                    model_name,
                    model_kind,
                    model_ts_ms,
                    regime,
                    actor,
                    reason,
                    json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                    json.dumps(previous_state, separators=(",", ":"), sort_keys=True) if previous_state else None,
                    json.dumps(new_state, separators=(",", ":"), sort_keys=True) if new_state else None,
                    int(time.time() * 1000)
                )
            )
            con.commit()
            logger.info(f"Recorded lineage event: {event_id}")
            return event_id
            
        finally:
            con.close()
    
    def record_training_data(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        regime: str,
        training_data: TrainingDataInfo
    ):
        """Record training data lineage"""
        con = connect()
        try:
            con.execute(
                """
                INSERT OR REPLACE INTO training_data_lineage
                (model_name, model_kind, model_ts_ms, regime, data_version, data_source,
                 data_hash, sample_count, feature_count, training_start_ts_ms, training_end_ts_ms,
                 preprocessing_config_json, data_quality_metrics_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    model_name,
                    model_kind,
                    model_ts_ms,
                    regime,
                    training_data.data_version,
                    training_data.data_source,
                    training_data.data_hash,
                    training_data.sample_count,
                    training_data.feature_count,
                    training_data.training_start_ts_ms,
                    training_data.training_end_ts_ms,
                    json.dumps(training_data.preprocessing_config, separators=(",", ":"), sort_keys=True),
                    json.dumps(training_data.data_quality_metrics, separators=(",", ":"), sort_keys=True)
                )
            )
            con.commit()
            
            # Record lineage event
            self.record_event(
                LineageEventType.DATA_VERSIONED,
                model_name, model_kind, model_ts_ms, regime,
                "system", "Training data versioned",
                {
                    "data_version": training_data.data_version,
                    "data_source": training_data.data_source,
                    "sample_count": training_data.sample_count,
                    "feature_count": training_data.feature_count
                }
            )
            
        finally:
            con.close()
    
    def add_model_relationship(
        self,
        parent_model: Dict[str, Any],
        child_model: Dict[str, Any],
        relationship_type: str,
        regime: str
    ):
        """Add parent-child model relationship"""
        con = connect()
        try:
            con.execute(
                """
                INSERT OR IGNORE INTO model_relationships
                (parent_model_name, parent_model_kind, parent_model_ts_ms,
                 child_model_name, child_model_kind, child_model_ts_ms,
                 relationship_type, regime, created_ts_ms)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    parent_model["model_name"],
                    parent_model["model_kind"],
                    parent_model["model_ts_ms"],
                    child_model["model_name"],
                    child_model["model_kind"],
                    child_model["model_ts_ms"],
                    relationship_type,
                    regime,
                    int(time.time() * 1000)
                )
            )
            con.commit()
            
        finally:
            con.close()
    
    def create_model_snapshot(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        regime: str,
        snapshot_type: str,
        state: Dict[str, Any],
        metrics: Optional[Dict[str, Any]] = None
    ) -> str:
        """Create a model state snapshot"""
        snapshot_id = f"{model_name}_{model_kind}_{model_ts_ms}_{snapshot_type}_{int(time.time() * 1000)}"
        
        con = connect()
        try:
            con.execute(
                """
                INSERT INTO model_snapshots
                (snapshot_id, model_name, model_kind, model_ts_ms, regime,
                 snapshot_type, snapshot_ts_ms, state_json, metrics_json, created_ts_ms)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    snapshot_id,
                    model_name,
                    model_kind,
                    model_ts_ms,
                    regime,
                    snapshot_type,
                    int(time.time() * 1000),
                    json.dumps(state, separators=(",", ":"), sort_keys=True),
                    json.dumps(metrics, separators=(",", ":"), sort_keys=True) if metrics else None,
                    int(time.time() * 1000)
                )
            )
            con.commit()
            return snapshot_id
            
        finally:
            con.close()
    
    def get_model_lineage(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        regime: str = "global"
    ) -> ModelLineage:
        """Get complete lineage for a specific model"""
        con = connect()
        try:
            # Get training data
            training_data_row = con.execute(
                """
                SELECT data_version, data_source, data_hash, sample_count, feature_count,
                       training_start_ts_ms, training_end_ts_ms, preprocessing_config_json,
                       data_quality_metrics_json
                FROM training_data_lineage
                WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
                """,
                (model_name, model_kind, model_ts_ms, regime)
            ).fetchone()
            
            training_data = None
            if training_data_row:
                training_data = TrainingDataInfo(
                    data_version=training_data_row[0],
                    data_source=training_data_row[1],
                    data_hash=training_data_row[2],
                    sample_count=training_data_row[3],
                    feature_count=training_data_row[4],
                    training_start_ts_ms=training_data_row[5],
                    training_end_ts_ms=training_data_row[6],
                    preprocessing_config=json.loads(training_data_row[7] or "{}"),
                    data_quality_metrics=json.loads(training_data_row[8] or "{}")
                )
            
            # Get events
            event_rows = con.execute(
                """
                SELECT event_id, ts_ms, event_type, actor, reason, metadata_json,
                       previous_state_json, new_state_json
                FROM model_lineage_events
                WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
                ORDER BY ts_ms ASC
                """,
                (model_name, model_kind, model_ts_ms, regime)
            ).fetchall()
            
            events = []
            for row in event_rows:
                events.append(ModelLineageEvent(
                    event_id=row[0],
                    ts_ms=row[1],
                    event_type=LineageEventType(row[2]),
                    model_name=model_name,
                    model_kind=model_kind,
                    model_ts_ms=model_ts_ms,
                    regime=regime,
                    actor=row[3],
                    reason=row[4],
                    metadata=json.loads(row[5] or "{}"),
                    previous_state=json.loads(row[6] or "{}") if row[6] else None,
                    new_state=json.loads(row[7] or "{}") if row[7] else None
                ))
            
            # Get parent models
            parent_rows = con.execute(
                """
                SELECT parent_model_name, parent_model_kind, parent_model_ts_ms, relationship_type
                FROM model_relationships
                WHERE child_model_name=? AND child_model_kind=? AND child_model_ts_ms=? AND regime=?
                """,
                (model_name, model_kind, model_ts_ms, regime)
            ).fetchall()
            
            parent_models = [
                {
                    "model_name": row[0],
                    "model_kind": row[1],
                    "model_ts_ms": row[2],
                    "relationship_type": row[3]
                }
                for row in parent_rows
            ]
            
            # Get child models
            child_rows = con.execute(
                """
                SELECT child_model_name, child_model_kind, child_model_ts_ms, relationship_type
                FROM model_relationships
                WHERE parent_model_name=? AND parent_model_kind=? AND parent_model_ts_ms=? AND regime=?
                """,
                (model_name, model_kind, model_ts_ms, regime)
            ).fetchall()
            
            child_models = [
                {
                    "model_name": row[0],
                    "model_kind": row[1],
                    "model_ts_ms": row[2],
                    "relationship_type": row[3]
                }
                for row in child_rows
            ]
            
            # Get current stage from model registry
            from engine.strategy.model_registry import get_stage_latest
            current_record = get_stage_latest(model_name, "champion", regime=regime)
            current_stage = ModelStage.CANDIDATE
            if current_record and current_record["model_kind"] == model_kind and current_record["model_ts_ms"] == model_ts_ms:
                current_stage = ModelStage.CHAMPION
            else:
                # Check other stages
                for stage in [ModelStage.CHALLENGER, ModelStage.SHADOW, ModelStage.RETIRED, ModelStage.QUARANTINED]:
                    record = get_stage_latest(model_name, stage.value, regime=regime)
                    if record and record["model_kind"] == model_kind and record["model_ts_ms"] == model_ts_ms:
                        current_stage = stage
                        break
            
            return ModelLineage(
                model_name=model_name,
                model_kind=model_kind,
                model_ts_ms=model_ts_ms,
                regime=regime,
                created_ts_ms=min([e.ts_ms for e in events]) if events else model_ts_ms,
                current_stage=current_stage,
                training_data=training_data,
                events=events,
                parent_models=parent_models,
                child_models=child_models,
                metrics_history=[],  # TODO: Implement metrics history
                promotion_chain=[]   # TODO: Implement promotion chain
            )
            
        finally:
            con.close()
    
    def explain_model_state(
        self,
        model_name: str,
        model_kind: str,
        model_ts_ms: int,
        regime: str = "global",
        as_of_ts_ms: Optional[int] = None
    ) -> str:
        """Generate human-readable explanation of why model is in current state"""
        lineage = self.get_model_lineage(model_name, model_kind, model_ts_ms, regime)
        
        if as_of_ts_ms:
            # Filter events up to specified time
            lineage.events = [e for e in lineage.events if e.ts_ms <= as_of_ts_ms]
        
        explanation = []
        explanation.append(f"## Model State Explanation")
        explanation.append(f"**Model:** {model_name} ({model_kind}:{model_ts_ms})")
        explanation.append(f"**Regime:** {regime}")
        explanation.append(f"**Current Stage:** {lineage.current_stage.value}")
        explanation.append(f"**Created:** {datetime.fromtimestamp(lineage.created_ts_ms/1000, tz=timezone.utc).isoformat()}")
        
        if lineage.training_data:
            explanation.append(f"\n### Training Data")
            explanation.append(f"- **Version:** {lineage.training_data.data_version}")
            explanation.append(f"- **Source:** {lineage.training_data.data_source}")
            explanation.append(f"- **Samples:** {lineage.training_data.sample_count:,}")
            explanation.append(f"- **Features:** {lineage.training_data.feature_count}")
            explanation.append(f"- **Training Period:** {datetime.fromtimestamp(lineage.training_data.training_start_ts_ms/1000, tz=timezone.utc).date()} to {datetime.fromtimestamp(lineage.training_data.training_end_ts_ms/1000, tz=timezone.utc).date()}")
        
        if lineage.events:
            explanation.append(f"\n### Key Events")
            for event in lineage.events[-10:]:  # Show last 10 events
                explanation.append(f"- **{datetime.fromtimestamp(event.ts_ms/1000, tz=timezone.utc).isoformat()}** - {event.event_type.value.replace('_', ' ').title()}")
                explanation.append(f"  - **Actor:** {event.actor}")
                explanation.append(f"  - **Reason:** {event.reason}")
                if event.metadata:
                    key_metadata = {k: v for k, v in event.metadata.items() if k in ['sharpe', 'win_rate', 'improvement', 'performance_score']}
                    if key_metadata:
                        explanation.append(f"  - **Key Metrics:** {', '.join([f'{k}: {v}' for k, v in key_metadata.items()])}")
        
        if lineage.parent_models:
            explanation.append(f"\n### Parent Models")
            for parent in lineage.parent_models:
                explanation.append(f"- {parent['model_name']} ({parent['model_kind']}:{parent['model_ts_ms']}) - {parent['relationship_type']}")
        
        if lineage.child_models:
            explanation.append(f"\n### Child Models")
            for child in lineage.child_models:
                explanation.append(f"- {child['model_name']} ({child['model_kind']}:{child['model_ts_ms']}) - {child['relationship_type']}")
        
        return "\n".join(explanation)
    
    def get_model_history(
        self,
        model_name: str,
        regime: str = "global",
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get history of all versions of a model"""
        con = connect()
        try:
            rows = con.execute(
                """
                SELECT DISTINCT model_kind, model_ts_ms
                FROM model_lineage_events
                WHERE model_name=? AND regime=?
                ORDER BY model_ts_ms DESC
                LIMIT ?
                """,
                (model_name, regime, limit)
            ).fetchall()
            
            history = []
            for model_kind, model_ts_ms in rows:
                lineage = self.get_model_lineage(model_name, model_kind, model_ts_ms, regime)
                history.append({
                    "model_kind": model_kind,
                    "model_ts_ms": model_ts_ms,
                    "current_stage": lineage.current_stage.value,
                    "created_ts_ms": lineage.created_ts_ms,
                    "event_count": len(lineage.events),
                    "has_training_data": lineage.training_data is not None
                })
            
            return history
            
        finally:
            con.close()

# Global lineage tracker instance
_lineage_tracker = None

def get_lineage_tracker() -> ModelLineageTracker:
    """Get singleton lineage tracker instance"""
    global _lineage_tracker
    if _lineage_tracker is None:
        _lineage_tracker = ModelLineageTracker()
    return _lineage_tracker
