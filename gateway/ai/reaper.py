import subprocess
import logging
import json
import time
import os
import re
from pathlib import Path
from typing import Dict, Any, List

logger = logging.getLogger("delimit.reaper")

def reap_agent_tasks(project_path: str = "/home/delimit/delimit-gateway") -> List[str]:
    """Reap completed agent arms by checking for commits and merging."""
    from ai.agent_dispatch import get_agent_status, complete_task
    from ai.ledger_manager import update_item
    
    # 1. Get all dispatched tasks
    res = get_agent_status()
    tasks = res.get("tasks", [])
    if not isinstance(tasks, list):
        tasks = list(tasks.values())
    
    reaped = []
    for task in tasks:
        if task.get("status") != "dispatched":
            continue
            
        task_id = task.get("id")
        desc = task.get("description", "")
        
        # 2. Extract branch from description
        match = re.search(r'branch ([\w/-]+)', desc)
        if not match:
            continue
        branch = match.group(1)
        
        # 3. Check for new commits on the branch
        try:
            cmd = ["git", "-C", project_path, "log", f"main..${branch}", "--oneline"]
            p = subprocess.run(cmd, capture_output=True, text=True)
            if p.returncode == 0 and p.stdout.strip():
                logger.info(f"Reaper: Found activity on ${branch} for task ${task_id}")
                
                # 4. RUN TESTS
                test_cmd = ["python3", "-m", "pytest", "tests/", "-v", "--maxfail=3"]
                env = {"DELIMIT_TEST_MODE": "1", "PATH": f"/root/.delimit/shims:${os.environ['PATH']}"}
                test_p = subprocess.run(test_cmd, cwd=project_path, capture_output=True, text=True, env={**os.environ, **env})
                
                if test_p.returncode == 0:
                    # 5. MERGE
                    subprocess.run(["git", "-C", project_path, "checkout", "main"], check=True)
                    subprocess.run(["git", "-C", project_path, "merge", branch], check=True)
                    
                    # 6. COMPLETE
                    complete_task(task_id, result="Reaper: Actually built and merged to main.")
                    
                    # 7. Update Ledger
                    ctx = task.get("context", "")
                    led_match = re.search(r'LED-(\d+)', ctx)
                    if led_match:
                        led_id = led_match.group(0)
                        update_item(item_id=led_id, status="done", note=f"Arm ${task_id} merged.")
                    
                    reaped.append(task_id)
                else:
                    logger.warning(f"Reaper: Tests failed for ${branch}, skipping merge.")
        except Exception as e:
            logger.warning(f"Reaper: Failed to reap task ${task_id}: ${e}")
            
    return reaped
