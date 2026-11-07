"""The environment procmon adds to a process it launches so its AI use is tracked (ai_shim/) and its levers apply."""
from __future__ import annotations

import logging
import os
import secrets

logger = logging.getLogger(__name__)

SHIM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_shim")
TOKEN_PATH = os.path.expanduser("~/.config/procmon/ai_token")


def client_token() -> str:
    """Authenticates the apps to /api/ai/sync. procmon is reachable through a public tunnel, so a loopback check
    wouldn't do (tunneled requests arrive from loopback too). A file, because restart_apps.py and hand-run restarts
    start processes without procmon's environment."""
    try:
        with open(TOKEN_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    try:
        fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:             # another starter won the race
        with open(TOKEN_PATH) as f:
            return f.read().strip()
    token = secrets.token_hex(32)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token


def env_for(process_id, env_vars: dict) -> dict:
    """Added to the launch environment. Empty on any failure: a process starting untracked beats not starting."""
    if not process_id:
        return {}
    try:
        from procmon.db import get_session
        from procmon.models import Process
        with get_session() as db:
            process = db.query(Process).filter_by(id=process_id).one_or_none()
            if process is None:
                return {}
            project_id, name = process.project_id, process.name
        inherited = (env_vars or {}).get("PYTHONPATH", os.environ.get("PYTHONPATH", ""))
        parts = [SHIM_DIR] + [p for p in inherited.split(os.pathsep) if p and p != SHIM_DIR]
        return {
            "PYTHONPATH": os.pathsep.join(parts),
            "PROCMON_AI_PROJECT": str(project_id),
            "PROCMON_AI_PROCESS": name,
            "PROCMON_AI_URL": f"http://127.0.0.1:{os.environ.get('PROCMON_PORT', '8600').split()[0]}",
            "PROCMON_AI_TOKEN": client_token(),
        }
    except Exception as e:
        logger.warning("no AI tracking for process %s: %s", process_id, e)
        return {}
