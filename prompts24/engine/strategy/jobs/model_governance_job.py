"""
Model Governance Orchestration Job
Coordinates all governance components in a continuous monitoring loop.
"""

import time
import logging
import schedule
from typing import Dict, Any

from engine.strategy.champion_challenger import get_champion_challenger_manager
from engine.strategy.automatic_demotion import get_demotion_manager
from engine.strategy.rollback_manager import get_rollback_manager
from engine.strategy.kill_switch_integration import get_kill_switch_integration
from engine.strategy.shadow_trading import get_shadow_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ModelGovernanceJob:
    """Main orchestration job for model governance"""
    
    def __init__(self):
        self.cc_manager = get_champion_challenger_manager()
        self.demotion_manager = get_demotion_manager()
        self.rollback_manager = get_rollback_manager()
        self.kill_switch = get_kill_switch_integration()
        self.shadow_manager = get_shadow_manager()
        
        # Schedule governance tasks
        self._schedule_tasks()
    
    def _schedule_tasks(self):
        """Schedule recurring governance tasks"""
        # Champion/challenger evaluation - every 6 hours
        schedule.every(6).hours.do(self.run_challenger_evaluation)
        
        # Model health monitoring - every 15 minutes
        schedule.every(15).minutes.do(self.run_health_monitoring)
        
        # Rollback candidate preparation - every hour
        schedule.every(1).hours.do(self.prepare_rollback_candidates)
        
        # Shadow trading evaluation - every 24 hours
        schedule.every(1).days.do(self.run_shadow_evaluation)
        
        # Kill-switch compatibility check - every 5 minutes
        schedule.every(5).minutes.do(self.run_kill_switch_checks)
        
        logger.info("Model governance job scheduled")
    
    def run_challenger_evaluation(self):
        """Run champion/challenger evaluation cycle"""
        try:
            logger.info("Starting challenger evaluation cycle")
            self.cc_manager.run_evaluation_cycle()
            logger.info("Challenger evaluation cycle completed")
        except Exception as e:
            logger.error(f"Challenger evaluation failed: {e}")
    
    def run_health_monitoring(self):
        """Run model health monitoring and automatic demotion"""
        try:
            logger.info("Starting health monitoring cycle")
            self.demotion_manager.monitor_all_models()
            logger.info("Health monitoring cycle completed")
        except Exception as e:
            logger.error(f"Health monitoring failed: {e}")
    
    def prepare_rollback_candidates(self):
        """Prepare rollback candidates for all models"""
        try:
            logger.info("Preparing rollback candidates")
            
            # Get all models with champions
            from engine.storage import connect
            con = connect()
            try:
                models = con.execute(
                    """
                    SELECT DISTINCT model_name, regime
                    FROM model_registry
                    WHERE stage='champion'
                    """
                ).fetchall()
                
                for model_name, regime in models:
                    self.rollback_manager.prepare_rollback_candidates(model_name, regime)
                
                logger.info(f"Prepared rollback candidates for {len(models)} models")
                
            finally:
                con.close()
                
        except Exception as e:
            logger.error(f"Rollback candidate preparation failed: {e}")
    
    def run_shadow_evaluation(self):
        """Run shadow trading evaluation"""
        try:
            logger.info("Starting shadow trading evaluation")
            
            # Get all shadow models
            from engine.storage import connect
            con = connect()
            try:
                shadow_models = con.execute(
                    """
                    SELECT DISTINCT model_name, model_kind, model_ts_ms, regime
                    FROM model_registry
                    WHERE stage='shadow'
                    """
                ).fetchall()
                
                for model_name, model_kind, model_ts_ms, regime in shadow_models:
                    result = self.shadow_manager.evaluate_shadow_performance(
                        model_name, model_kind, model_ts_ms, regime
                    )
                    logger.info(f"Shadow evaluation for {model_name}: {result.get('status', 'unknown')}")
                
            finally:
                con.close()
                
        except Exception as e:
            logger.error(f"Shadow evaluation failed: {e}")
    
    def run_kill_switch_checks(self):
        """Run kill-switch compatibility checks"""
        try:
            logger.info("Running kill-switch compatibility checks")
            
            # Get all champion models
            from engine.storage import connect
            con = connect()
            try:
                models = con.execute(
                    """
                    SELECT DISTINCT model_name, regime
                    FROM model_registry
                    WHERE stage='champion'
                    """
                ).fetchall()
                
                for model_name, regime in models:
                    compatibility = self.kill_switch.check_kill_switch_compatibility(model_name, regime)
                    if not compatibility.get("compatible", True):
                        logger.warning(f"Kill-switch incompatibility detected for {model_name}: {compatibility.get('reason')}")
                
            finally:
                con.close()
                
        except Exception as e:
            logger.error(f"Kill-switch checks failed: {e}")
    
    def run_manual_cycle(self, task_type: str = "all"):
        """Run a manual governance cycle"""
        if task_type == "all" or task_type == "challenger":
            self.run_challenger_evaluation()
        
        if task_type == "all" or task_type == "health":
            self.run_health_monitoring()
        
        if task_type == "all" or task_type == "rollback":
            self.prepare_rollback_candidates()
        
        if task_type == "all" or task_type == "shadow":
            self.run_shadow_evaluation()
        
        if task_type == "all" or task_type == "killswitch":
            self.run_kill_switch_checks()
    
    def start(self):
        """Start the governance job scheduler"""
        logger.info("Starting model governance job scheduler")
        
        while True:
            try:
                schedule.run_pending()
                time.sleep(60)  # Check every minute
            except KeyboardInterrupt:
                logger.info("Model governance job stopped by user")
                break
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                time.sleep(300)  # Wait 5 minutes on error

def run_governance_job():
    """Entry point for running the governance job"""
    job = ModelGovernanceJob()
    job.start()

if __name__ == "__main__":
    run_governance_job()
