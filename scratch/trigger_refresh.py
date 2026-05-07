import sys
import os
import subprocess
from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "."))

def run_git_pull():
    logger.info("Performing git pull origin main...")
    try:
        result = subprocess.run(["git", "pull", "origin", "main"], capture_output=True, text=True)
        if result.returncode == 0:
            logger.success(f"Git pull successful:\n{result.stdout}")
        else:
            logger.error(f"Git pull failed:\n{result.stderr}")
    except Exception as e:
        logger.error(f"Git pull error: {e}")

def trigger_all():
    from core.strategy_registry import strategy_registry
    logger.info("Triggering level refresh for all strategies...")
    for strategy in strategy_registry.list():
        try:
            logger.info(f"Refreshing {strategy.name}...")
            strategy.fetch_now()
            logger.success(f"Refreshed {strategy.name}")
        except Exception as e:
            logger.error(f"Failed to refresh {strategy.name}: {e}")

if __name__ == "__main__":
    run_git_pull()
    trigger_all()
