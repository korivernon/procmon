"""machine.py: paths, database rewriting and role. Run:  cd ~/Development && python3 procmon/tests/machine_check.py"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from procmon import machine as m

with tempfile.TemporaryDirectory() as root:
    root = os.path.realpath(root)
    os.makedirs(f"{root}/pomtrader"); os.makedirs(f"{root}/everything")
    open(f"{root}/pomtrader/.env", "w").write(
        'POMTRADER_DATABASE_URL=postgresql+psycopg2://admin@127.0.0.1:5433/postgres\n'
        'export OTHER="postgresql://u:p@db.example.com:5432/x"\nSECRET=abc\n')
    open(f"{root}/everything/.env", "w").write("EVERYTHING_DATABASE_URL='postgresql+psycopg://admin:pw@localhost/postgres?sslmode=disable'\n")
    os.environ.update(PROCMON_PROJECTS_ROOT=root, PROCMON_PYTHON="/usr/bin/python3")
    for k in ("PROCMON_DB_HOST", "PROCMON_DB_PORT", "PROCMON_ROLE"):
        os.environ.pop(k, None)

    # paths round-trip, and are left alone when already portable
    mac = f"{root}/pomtrader/.venv/bin/python3 -m pomtrader.dashboard.app"
    assert m.portable(mac) == "{root}/pomtrader/.venv/bin/python3 -m pomtrader.dashboard.app"
    assert m.expand(m.portable(mac)) == mac and m.portable(m.portable(mac)) == m.portable(mac)
    brew = (f"PYTHONPATH={root} /opt/homebrew/bin/python3 pomtrader/ops/restart_apps.py --wait 0 \"pomtrader dashboard\"")
    assert m.portable(brew) == 'PYTHONPATH={root} {python} pomtrader/ops/restart_apps.py --wait 0 "pomtrader dashboard"'
    cellar = ("/opt/homebrew/Cellar/python@3.11/3.11.5/Frameworks/Python.framework/Versions/3.11/Resources/Python.app/"
              f"Contents/MacOS/Python {root}/etrade-monitor/emon_main.py")
    assert m.portable(cellar) == "{python} {root}/etrade-monitor/emon_main.py"
    assert m.expand("{python} {root}/x.py") == f"/usr/bin/python3 {root}/x.py"
    assert m.portable(f"{root}other/x") == f"{root}other/x"            # a sibling folder that only shares the prefix
    assert m.portable(".venv/bin/python run.py --port 1250") == ".venv/bin/python run.py --port 1250"
    assert m.portable(None) is None and m.expand(None) is None

    # no target: nothing is touched
    assert m.db_env(m.portable(mac), None) == {} and m.db_target() is None

    # a target: local addresses move, remote ones and non-database settings don't
    os.environ["PROCMON_DB_PORT"] = "5544"
    env = m.db_env(m.portable(mac), None)
    assert env == {"POMTRADER_DATABASE_URL": "postgresql+psycopg2://admin@127.0.0.1:5544/postgres", "PGPORT": "5544"}, env
    os.environ["PROCMON_DB_HOST"] = "10.0.0.5"
    env = m.db_env("{root}/everything/.venv/bin/python run.py", "{root}/everything")
    assert env["EVERYTHING_DATABASE_URL"] == "postgresql+psycopg://admin:pw@10.0.0.5:5544/postgres?sslmode=disable", env
    assert env["PGHOST"] == "10.0.0.5"
    env = m.db_env("x", None, {"MY_DATABASE_URL": "postgresql://admin@127.0.0.1:5433/postgres"})
    assert env["MY_DATABASE_URL"] == "postgresql://admin@10.0.0.5:5544/postgres"
    assert m.rewrite_url("postgresql://u:p@db.example.com:5432/x", "h", "1") == "postgresql://u:p@db.example.com:5432/x"
    assert m.rewrite_url("sqlite:///x.db", "h", "1") == "sqlite:///x.db"
    assert m.rewrite_url("postgresql://admin@127.0.0.1/postgres", None, "6000") == "postgresql://admin@127.0.0.1:6000/postgres"

    # role
    assert m.allowed(True, None) == (True, "") and m.role() == "live"
    os.environ["PROCMON_ROLE"] = "standby"
    assert not m.allowed(True, None)[0] and m.allowed(False, None)[0]
    other = "linux" if m.platform() == "darwin" else "darwin"
    assert not m.allowed(False, other)[0] and m.allowed(False, m.platform())[0]
    full = m.env_for(m.portable(mac), None)
    assert full["PROCMON_ROLE"] == "standby" and full["PROCMON_PROJECTS_ROOT"] == root
print("ALL PASSED")
