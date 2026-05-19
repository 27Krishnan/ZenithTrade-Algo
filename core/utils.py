import os
import platform
import subprocess
import pytz
from datetime import datetime
from loguru import logger

IST = pytz.timezone("Asia/Kolkata")

def get_now_ist() -> datetime:
    """Returns current datetime in IST (Asia/Kolkata)"""
    return datetime.now(IST)

def auto_pull_latest(strategy_name: str = "System"):
    """
    7:55 AM — Pull latest CSV data from GitHub to ensure morning briefing is accurate.
    Works on both Windows (local) and Linux (GCP).
    """
    logger.info(f"{strategy_name}: Auto-pulling latest data from GitHub...")
    try:
        git_cmd = "git"
        
        # If on Windows, try to find the specific GitHub Desktop path for the user
        if platform.system() == "Windows":
            potential_git = r"C:\Users\Krishnan\AppData\Local\GitHubDesktop\app-3.5.8\resources\app\git\cmd\git.exe"
            if os.path.exists(potential_git):
                git_cmd = potential_git
        
        # Determine the project root (one level up from 'core/')
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        
        fetch_res = subprocess.run(
            [git_cmd, "fetch", "origin", "main"], 
            cwd=project_root,
            capture_output=True, 
            text=True
        )
        if fetch_res.returncode != 0:
            logger.error(f"{strategy_name}: Git fetch failed: {fetch_res.stderr}")
            return
            
        reset_res = subprocess.run(
            [git_cmd, "--no-pager", "reset", "--hard", "origin/main"],
            cwd=project_root,
            capture_output=True,
            text=True
        )
        if reset_res.returncode == 0:
            logger.info(f"{strategy_name}: Git reset successful: {reset_res.stdout.strip()}")
        else:
            logger.error(f"{strategy_name}: Git reset failed: {reset_res.stderr}")
            
    except Exception as e:
        logger.error(f"{strategy_name}: Git pull error: {e}")
