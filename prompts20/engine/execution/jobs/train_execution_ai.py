# engine/execution/jobs/train_execution_ai.py
"""
Execution AI Training Job

Periodically trains the execution AI model with new data.
Runs as a background job to continuously improve the model.
"""

import time
import json
from typing import Dict, Any

from engine.storage import connect
from engine.execution.execution_ai_model import train_execution_ai_model, is_model_trained
from engine.execution.execution_ai_dashboard import get_execution_ai_dashboard


def _ensure_training_tables(con) -> None:
    """Create tables for training job management"""
    con.executescript("""
    CREATE TABLE IF NOT EXISTS execution_ai_training_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        training_ts_ms INTEGER NOT NULL,
        model_version TEXT NOT NULL,
        status TEXT NOT NULL,
        sample_count INTEGER,
        train_mae REAL,
        val_mae REAL,
        train_rmse REAL,
        val_rmse REAL,
        training_duration_ms INTEGER,
        error_message TEXT,
        created_ts_ms INTEGER NOT NULL
    );
    
    CREATE INDEX IF NOT EXISTS idx_ai_training_ts ON execution_ai_training_log(training_ts_ms);
    CREATE INDEX IF NOT EXISTS idx_ai_training_status ON execution_ai_training_log(status);
    
    -- Training schedule
    CREATE TABLE IF NOT EXISTS execution_ai_training_schedule (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        last_training_ts_ms INTEGER,
        next_training_ts_ms INTEGER,
        training_interval_hours INTEGER DEFAULT 24,
        min_samples_required INTEGER DEFAULT 100,
        auto_train_enabled INTEGER DEFAULT 1,
        created_ts_ms INTEGER NOT NULL,
        updated_ts_ms INTEGER NOT NULL
    );
    """)


def get_training_schedule() -> Dict[str, Any]:
    """Get current training schedule"""
    con = connect()
    try:
        _ensure_training_tables(con)
        
        row = con.execute("""
            SELECT last_training_ts_ms, next_training_ts_ms, training_interval_hours,
                   min_samples_required, auto_train_enabled
            FROM execution_ai_training_schedule
            ORDER BY id DESC
            LIMIT 1
        """).fetchone()
        
        if not row:
            # Create default schedule
            now_ms = int(time.time() * 1000)
            con.execute("""
                INSERT INTO execution_ai_training_schedule(
                    last_training_ts_ms, next_training_ts_ms, training_interval_hours,
                    min_samples_required, auto_train_enabled, created_ts_ms, updated_ts_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (0, now_ms + (24 * 3600 * 1000), 24, 100, 1, now_ms, now_ms))
            con.commit()
            
            return {
                'last_training_ts_ms': 0,
                'next_training_ts_ms': now_ms + (24 * 3600 * 1000),
                'training_interval_hours': 24,
                'min_samples_required': 100,
                'auto_train_enabled': True
            }
        
        return {
            'last_training_ts_ms': int(row[0] or 0),
            'next_training_ts_ms': int(row[1] or 0),
            'training_interval_hours': int(row[2] or 24),
            'min_samples_required': int(row[3] or 100),
            'auto_train_enabled': bool(row[4] or 0)
        }
        
    finally:
        con.close()


def update_training_schedule(
    training_interval_hours: Optional[int] = None,
    min_samples_required: Optional[int] = None,
    auto_train_enabled: Optional[bool] = None
) -> bool:
    """Update training schedule"""
    con = connect()
    try:
        _ensure_training_tables(con)
        
        now_ms = int(time.time() * 1000)
        
        # Get current schedule
        current = get_training_schedule()
        
        # Update fields
        updates = []
        params = []
        
        if training_interval_hours is not None:
            updates.append("training_interval_hours = ?")
            params.append(training_interval_hours)
            # Update next training time
            next_training = current['last_training_ts_ms'] + (training_interval_hours * 3600 * 1000)
            if next_training <= now_ms:
                next_training = now_ms + (training_interval_hours * 3600 * 1000)
            updates.append("next_training_ts_ms = ?")
            params.append(next_training)
        
        if min_samples_required is not None:
            updates.append("min_samples_required = ?")
            params.append(min_samples_required)
        
        if auto_train_enabled is not None:
            updates.append("auto_train_enabled = ?")
            params.append(1 if auto_train_enabled else 0)
        
        if updates:
            updates.append("updated_ts_ms = ?")
            params.append(now_ms)
            params.append(now_ms)  # for WHERE clause
            
            con.execute(f"""
                UPDATE execution_ai_training_schedule
                SET {', '.join(updates)}
                WHERE created_ts_ms = (SELECT MAX(created_ts_ms) FROM execution_ai_training_schedule)
            """, params)
            con.commit()
            return True
        
        return False
        
    finally:
        con.close()


def check_and_train_if_needed() -> Dict[str, Any]:
    """Check if training is needed and execute if conditions are met"""
    
    schedule = get_training_schedule()
    now_ms = int(time.time() * 1000)
    
    result = {
        'action': 'none',
        'reason': '',
        'training_result': None
    }
    
    # Check if auto-training is enabled
    if not schedule['auto_train_enabled']:
        result['reason'] = 'Auto-training is disabled'
        return result
    
    # Check if it's time to train
    if now_ms < schedule['next_training_ts_ms']:
        result['reason'] = f'Not time to train yet. Next training at {schedule["next_training_ts_ms"]}'
        return result
    
    # Check minimum samples
    from engine.execution.execution_ai_features import get_training_data
    
    X, y = get_training_data(lookback_days=30, min_samples=0)  # Get all available data
    available_samples = len(X)
    
    if available_samples < schedule['min_samples_required']:
        result['reason'] = f'Insufficient samples: {available_samples} < {schedule["min_samples_required"]}'
        return result
    
    # Execute training
    result['action'] = 'training'
    training_result = train_execution_ai_job(
        lookback_days=30,
        min_samples=schedule['min_samples_required']
    )
    result['training_result'] = training_result
    
    # Update schedule
    con = connect()
    try:
        _ensure_training_tables(con)
        
        next_training = now_ms + (schedule['training_interval_hours'] * 3600 * 1000)
        con.execute("""
            UPDATE execution_ai_training_schedule
            SET last_training_ts_ms = ?, next_training_ts_ms = ?, updated_ts_ms = ?
            WHERE created_ts_ms = (SELECT MAX(created_ts_ms) FROM execution_ai_training_schedule)
        """, (now_ms, next_training, now_ms))
        con.commit()
        
    finally:
        con.close()
    
    return result


def train_execution_ai_job(lookback_days: int = 30, min_samples: int = 100) -> Dict[str, Any]:
    """Execute execution AI training job"""
    
    start_time_ms = int(time.time() * 1000)
    
    training_log = {
        'training_ts_ms': start_time_ms,
        'model_version': f'v{start_time_ms}',
        'status': 'started',
        'sample_count': 0,
        'train_mae': 0.0,
        'val_mae': 0.0,
        'train_rmse': 0.0,
        'val_rmse': 0.0,
        'training_duration_ms': 0,
        'error_message': None
    }
    
    con = connect()
    try:
        _ensure_training_tables(con)
        
        # Log training start
        con.execute("""
            INSERT INTO execution_ai_training_log(
                training_ts_ms, model_version, status, created_ts_ms
            ) VALUES (?, ?, ?, ?)
        """, (training_log['training_ts_ms'], training_log['model_version'], 
              training_log['status'], start_time_ms))
        con.commit()
        
        # Execute training
        try:
            metrics = train_execution_ai_model(lookback_days, min_samples)
            
            training_log.update({
                'status': 'completed',
                'sample_count': metrics.sample_count,
                'train_mae': metrics.train_mae,
                'val_mae': metrics.val_mae,
                'train_rmse': metrics.train_rmse,
                'val_rmse': metrics.val_rmse
            })
            
        except Exception as e:
            training_log.update({
                'status': 'failed',
                'error_message': str(e)
            })
        
        # Calculate duration
        end_time_ms = int(time.time() * 1000)
        training_log['training_duration_ms'] = end_time_ms - start_time_ms
        
        # Update training log
        con.execute("""
            UPDATE execution_ai_training_log
            SET status = ?, sample_count = ?, train_mae = ?, val_mae = ?,
                train_rmse = ?, val_rmse = ?, training_duration_ms = ?, error_message = ?
            WHERE training_ts_ms = ?
        """, (
            training_log['status'], training_log['sample_count'],
            training_log['train_mae'], training_log['val_mae'],
            training_log['train_rmse'], training_log['val_rmse'],
            training_log['training_duration_ms'], training_log['error_message'],
            training_log['training_ts_ms']
        ))
        con.commit()
        
        return {
            'ok': training_log['status'] == 'completed',
            'training_log': training_log,
            'message': f"Training {training_log['status']}"
        }
        
    except Exception as e:
        return {
            'ok': False,
            'error': str(e),
            'message': "Training job failed"
        }
        
    finally:
        con.close()


def get_training_history(limit: int = 20) -> List[Dict[str, Any]]:
    """Get training history"""
    con = connect()
    try:
        _ensure_training_tables(con)
        
        rows = con.execute("""
            SELECT training_ts_ms, model_version, status, sample_count,
                   train_mae, val_mae, train_rmse, val_rmse, training_duration_ms, error_message
            FROM execution_ai_training_log
            ORDER BY training_ts_ms DESC
            LIMIT ?
        """, (limit,)).fetchall()
        
        history = []
        for row in rows:
            history.append({
                'training_ts_ms': int(row[0]),
                'model_version': row[1],
                'status': row[2],
                'sample_count': int(row[3]) if row[3] else 0,
                'train_mae': float(row[4]) if row[4] else 0,
                'val_mae': float(row[5]) if row[5] else 0,
                'train_rmse': float(row[6]) if row[6] else 0,
                'val_rmse': float(row[7]) if row[7] else 0,
                'training_duration_ms': int(row[8]) if row[8] else 0,
                'error_message': row[9]
            })
        
        return history
        
    finally:
        con.close()


def force_training(lookback_days: int = 30, min_samples: int = 100) -> Dict[str, Any]:
    """Force training regardless of schedule"""
    return train_execution_ai_job(lookback_days, min_samples)


def get_training_status() -> Dict[str, Any]:
    """Get current training status"""
    schedule = get_training_schedule()
    history = get_training_history(limit=1)
    
    status = {
        'schedule': schedule,
        'is_trained': is_model_trained(),
        'last_training': history[0] if history else None,
        'next_training_due': schedule['next_training_ts_ms'],
        'auto_training_enabled': schedule['auto_train_enabled']
    }
    
    # Check if training is needed
    now_ms = int(time.time() * 1000)
    status['training_needed'] = (
        schedule['auto_train_enabled'] and 
        now_ms >= schedule['next_training_ts_ms']
    )
    
    return status


# Main job function for scheduled execution
def run_execution_ai_training_job() -> Dict[str, Any]:
    """Main entry point for scheduled training job"""
    
    try:
        result = check_and_train_if_needed()
        
        # Log job execution
        con = connect()
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS job_execution_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_name TEXT NOT NULL,
                    execution_ts_ms INTEGER NOT NULL,
                    result_json TEXT,
                    status TEXT NOT NULL
                )
            """)
            
            con.execute("""
                INSERT INTO job_execution_log(job_name, execution_ts_ms, result_json, status)
                VALUES (?, ?, ?, ?)
            """, (
                'execution_ai_training',
                int(time.time() * 1000),
                json.dumps(result, separators=(',', ':'), sort_keys=True),
                'completed' if result['action'] != 'failed' else 'failed'
            ))
            con.commit()
            
        finally:
            con.close()
        
        return result
        
    except Exception as e:
        return {
            'action': 'failed',
            'error': str(e),
            'message': 'Training job execution failed'
        }
