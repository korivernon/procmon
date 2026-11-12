#!/usr/bin/python3
# ----------------------------------------------------------------------------
# Module     : procmon/machine.py
# Description: What differs between the machines procmon can run on, in one place, so the same process list works on
#              the MacBook and on the Windows server (Ubuntu under WSL2). Asked 11/09: "generalize the setup so that
#              ... I can run procmon off of the windows server/desktop and all of my projects will smoothly run".
#
#              Set in procmon's own .env (all optional; unset = today's MacBook behavior):
#                PROCMON_MACHINE        a name for this machine, shown in the UI and in alerts
#                PROCMON_PROJECTS_ROOT  where the project folders live (default: the folder procmon's own folder is in)
#                PROCMON_PYTHON         the base python for commands that say {python} (default: the one running procmon)
#                PROCMON_DB_HOST/_PORT  where this machine's Postgres listens (default: leave every project's own
#                                       address alone)
#                PROCMON_ROLE           live | standby. Only one machine is live. On a standby machine a process
#                                       marked live_only (the trading engines) is never started.
#
#              Three jobs:
#                1. Paths. A stored command or working dir says {root} for the projects folder and {python} for the
#                   base interpreter; expand() fills them in for this machine, portable() turns this machine's
#                   absolute paths back into the tokens when a command is saved.
#                2. Database. Every project reads its full database address from its own setting
#                   (POMTRADER_DATABASE_URL, EVERYTHING_DATABASE_URL, ...) in its .env. db_env() reads those, swaps in
#                   this machine's host and port, and hands them to the process procmon starts. A variable given by
#                   the launcher wins over the .env file, so no project needs its own port option.
#                3. Role. allowed() says whether a process may run here (live_only, platform).
# ----------------------------------------------------------------------------

from __future__ import annotations

import os
import re
import shlex
import socket
import sys

ROOT_TOKEN, PYTHON_TOKEN = "{root}", "{python}"

_HERE = os.path.dirname(os.path.abspath(__file__))


def name() -> str:
    return os.environ.get("PROCMON_MACHINE") or socket.gethostname().split(".")[0]


def root() -> str:
    return os.path.abspath(os.path.expanduser(os.environ.get("PROCMON_PROJECTS_ROOT") or os.path.dirname(_HERE)))


def python() -> str:
    return os.environ.get("PROCMON_PYTHON") or sys.executable


def role() -> str:
    r = (os.environ.get("PROCMON_ROLE") or "live").strip().lower()
    return r if r in ("live", "standby") else "live"


def platform() -> str:
    return ("darwin" if sys.platform == "darwin" else "linux" if sys.platform.startswith("linux")
            else "windows" if os.name == "nt" else sys.platform)


def log_dir() -> str:
    """Where process logs go: PROCMON_LOG_DIR, else procmon_logs in this machine's temp folder."""
    import tempfile
    return os.environ.get("PROCMON_LOG_DIR") or os.path.join(
        "/tmp" if os.name != "nt" else tempfile.gettempdir(), "procmon_logs")


# ---- paths ------------------------------------------------------------------------------------------------------------

def expand(text):
    """A stored command or directory, with this machine's paths filled in."""
    if not text:
        return text
    return text.replace(ROOT_TOKEN, root()).replace(PYTHON_TOKEN, python())


# Interpreters that only exist on one machine: the Homebrew framework build and its shims. A venv's own python is
# under {root} and needs no token.
_BASE_PYTHONS = re.compile(r"(?:/opt/homebrew|/usr/local)/(?:Cellar/python@[\d.]+/[\d._]+/Frameworks/Python\.framework/"
                           r"Versions/[\d.]+/Resources/Python\.app/Contents/MacOS/Python|bin/python3(?:\.\d+)?)(?=\s|$)")


def portable(text):
    """A command or directory as it should be stored: this machine's projects folder becomes {root}, a base
    interpreter becomes {python}."""
    if not text:
        return text
    r = root()
    out = re.sub(re.escape(r) + r"(?=[/\\]|\s|$|\"|')", ROOT_TOKEN, text)
    return _BASE_PYTHONS.sub(PYTHON_TOKEN, out)


# ---- database ---------------------------------------------------------------------------------------------------------

_PG_URL = re.compile(r"^(postgres(?:ql)?(?:\+\w+)?://)([^@/]*@)?(\[[^\]]+\]|[^:/?@]*)(?::(\d+))?(.*)$")
_LOCAL = {"127.0.0.1", "localhost", "::1", "[::1]", ""}


def db_target():
    """(host, port) this machine's projects should use, or None to leave every address alone."""
    host, port = os.environ.get("PROCMON_DB_HOST", "").strip(), os.environ.get("PROCMON_DB_PORT", "").strip()
    return (host or None, port or None) if (host or port) else None


def rewrite_url(url, host=None, port=None):
    """A Postgres address pointed at this machine's server. Only local addresses are touched: a project that talks
    to a database somewhere else keeps doing so."""
    m = _PG_URL.match((url or "").strip())
    if not m or m.group(3).lower() not in _LOCAL:
        return url
    scheme, creds, old_host, old_port, rest = m.groups()
    new_host, new_port = host or old_host or "127.0.0.1", port or old_port
    return f"{scheme}{creds or ''}{new_host}{':' + new_port if new_port else ''}{rest}"


def _env_file(path):
    """KEY -> value from a .env file (no shell expansion; quotes stripped). {} when it can't be read."""
    out = {}
    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key.startswith("export "):
                    key = key[7:].strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                    out[key] = value
    except OSError:
        pass
    return out


def project_dirs(command, working_dir):
    """The folders whose .env a process reads: its working dir, and the project folder of any path in its command
    (pomtrader's processes have no working dir; their interpreter path names the project)."""
    r = root()
    dirs = []
    if working_dir:
        dirs.append(expand(working_dir))
    try:
        tokens = shlex.split(expand(command or ""), posix=os.name != "nt")
    except ValueError:
        tokens = (expand(command or "")).split()
    for tok in tokens:
        tok = tok.split("=", 1)[-1].strip('"')            # PYTHONPATH=/x/y style
        if tok.startswith(r + os.sep) or tok.startswith(r + "/"):
            first = re.split(r"[/\\]", tok[len(r) + 1:])[0]
            if first:
                dirs.append(os.path.join(r, first))
    seen, out = set(), []
    for d in dirs:
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


def db_env(command, working_dir, env_vars=None):
    """The database settings to hand a process so it uses this machine's Postgres: each Postgres address found in
    its project's .env (or in its own env_vars), pointed at PROCMON_DB_HOST/PORT, plus PGHOST/PGPORT for psql-style
    tools. {} when no target is set."""
    target = db_target()
    if not target:
        return {}
    host, port = target
    out = {}
    sources = [_env_file(os.path.join(d, ".env")) for d in project_dirs(command, working_dir)]
    sources.append(dict(env_vars or {}))                   # the process's own settings win
    for src in sources:
        for key, value in src.items():
            if isinstance(value, str) and _PG_URL.match(value.strip()):
                new = rewrite_url(value, host, port)
                if new != value or key in out:
                    out[key] = new
    if host:
        out["PGHOST"] = host
    if port:
        out["PGPORT"] = port
    return out


def env_for(command, working_dir, env_vars=None):
    """Everything machine-specific a started process is told."""
    return {**db_env(command, working_dir, env_vars), "PROCMON_MACHINE": name(), "PROCMON_ROLE": role(),
            "PROCMON_PROJECTS_ROOT": root()}


# ---- role -------------------------------------------------------------------------------------------------------------

def allowed(live_only=False, only_platform=None):
    """(ok, why not). A live_only process runs only on the live machine; a platform-bound one (the iMessage relay
    needs a Mac) only on that platform."""
    if only_platform and only_platform != platform():
        return False, f"runs only on {only_platform}; this machine ({name()}) is {platform()}"
    if live_only and role() != "live":
        return False, f"{name()} is on standby: this process runs only on the live machine"
    return True, ""
