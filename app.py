#!/usr/bin/python3
# ----------------------------------------------------------------------------
# Module     : procmon/app.py
# Last updated: 2026-08-21 03:15 EDT
# Description: Process monitor web app -- separate tool from pomtrader,
#              sharing only the underlying Postgres instance (via a
#              separate "procmon" schema, see db.py).
#
#              DEPLOYMENT / SECURITY NOTE, worth reading before exposing
#              this anywhere reachable "on the go": this tool executes
#              arbitrary shell commands on request (that's the whole
#              point -- starting/stopping/restarting scripts). Reaching
#              it is equivalent to having a terminal open on this
#              machine. The password check in auth.py is real, but a
#              single password alone is NOT sufficient protection for
#              something with this capability if exposed directly to the
#              open internet -- put this behind something like
#              Tailscale, a Cloudflare Tunnel, or a VPN, so "reachable
#              at all" already implies "a device you trust", rather than
#              relying on the password as the only barrier between the
#              open internet and shell access to this machine.
#
#              UPDATED: two genuinely new capabilities, not present
#              before --
#
#              1) "Perpetually stay up" is now a REAL, working behavior,
#                 not just detection. Previously, the reconciliation
#                 loop only LOGGED a crash event for a perpetual process
#                 that died unexpectedly -- it never actually restarted
#                 anything. It now does, WITH a crash-loop backoff: a
#                 process crashing repeatedly in a short window stops
#                 being auto-restarted and is flagged explicitly, rather
#                 than being relaunched every ~15 seconds forever. That
#                 backoff is a real, deliberate safety mechanism, not an
#                 afterthought -- an unthrottled auto-restart loop
#                 against a genuinely broken command would otherwise
#                 spam both the log file and the process table
#                 indefinitely.
#
#              2) "Run on a schedule" is new -- process.run_mode can now
#                 be "scheduled", with a standard cron expression
#                 (process.schedule_cron), using APScheduler's own
#                 CronTrigger for both validation and the actual
#                 triggering -- not hand-rolled cron parsing. A scheduled
#                 run uses process_manager.run_one_shot(), which is
#                 confirmed race-free for capturing a fast command's
#                 real exit code (see its own docstring for the specific
#                 race that a naive start-then-separately-wait approach
#                 has, confirmed directly in testing before this).
#
# Env vars:
#   PROCMON_PASSWORD       -- required, the single login password
#   FLASK_SECRET_KEY       -- required for session cookies to be secure;
#                              generate one with: python3 -c "import secrets; print(secrets.token_hex(32))"
#   PROCMON_DATABASE_URL   -- optional, see db.py for the default
#   PROCMON_BASE_URL       -- optional, used to build the link sent in a crash
#                              notification (e.g. "https://procmon.example.com");
#                              defaults to http://127.0.0.1:$PROCMON_PORT, which
#                              is only reachable from this machine itself
#
#   Crash notifications via photon-notif (photon-notif/ here is a symlink
#   to pomtrader's own photon-notif -- the same Spectrum iMessage relay,
#   same phone line, shared with pomtrader's ai_gerry trade-approval
#   texts). All optional; if PROCMON_NOTIFY_PHONE or PHOTON_SHARED_SECRET
#   is unset, crash notifications are silently skipped (auto-restart /
#   crash-loop backoff below still work exactly as before, just without a
#   text). The text is informational only (process name + a link to its
#   page) -- deliberately no "reply to restart" action, since that would
#   collide with pomtrader's own reply-driven approval flow on the same
#   shared line; restarting a crashed process is a click on the page the
#   link points to, not a text reply.
#   PROCMON_NOTIFY_PHONE   -- phone number (or handle) to text on a crash
#   PHOTON_NOTIF_URL       -- optional, defaults to http://127.0.0.1:8790
#   PHOTON_SHARED_SECRET   -- must match photon-notif/.env's own copy;
#                              authenticates the outbound /send call
#   PROCMON_NOTIFY_EMAIL   -- also email this address on a crash (see
#                              emailer.py -- an independent channel for
#                              when photon-notif is itself down)
# ----------------------------------------------------------------------------

from __future__ import annotations

import logging
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed -- fall back to already-exported
            # shell environment variables, same graceful-degradation
            # pattern used throughout this session's other scripts
import threading
import time
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from sqlalchemy import func, text

from procmon import emailer
from procmon import photon_client
from procmon import process_manager as pm
from procmon import usage_tracker
from procmon.auth import check_password, login_required
from procmon.db import get_session, init_db
from procmon.models import Process, ProcessEvent, Project, UsageSample

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError(
        "FLASK_SECRET_KEY must be set -- generate one with: "
        "python3 -c \"import secrets; print(secrets.token_hex(32))\""
    )

RECONCILE_INTERVAL_SECONDS = 15
CRASH_LOOP_WINDOW_MINUTES = 5
CRASH_LOOP_THRESHOLD = 3  # this many crashes within the window above -> stop auto-restarting

PROCMON_BASE_URL = os.environ.get("PROCMON_BASE_URL", f"http://127.0.0.1:{os.environ.get('PROCMON_PORT', 8600)}").rstrip("/")
PROCMON_NOTIFY_PHONE = os.environ.get("PROCMON_NOTIFY_PHONE", "")

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()


def _process_page_link(project_id: str) -> str:
    # Built by hand, NOT via url_for -- confirmed a real, severe failure:
    # url_for outside an active request raises RuntimeError unless
    # SERVER_NAME is configured (app_context alone is not enough on this
    # Flask version), and this is called from the reconcile thread's
    # _notify_crash BEFORE its per-channel try/excepts -- so the raise
    # escaped into the tick's own catch-all, rolling back the crash event
    # and skipping the restart. Net effect: any crash that warranted a
    # notification was silently un-detected, forever. A hardcoded path
    # matching project_detail's route is boring and cannot fail.
    return f"{PROCMON_BASE_URL}/projects/{project_id}"


def _notify_crash(process: Process, project_name: str) -> None:
    """
    Texts PROCMON_NOTIFY_PHONE via photon-notif that a process crashed,
    with a link to its project page -- informational only, no reply
    action. (photon-notif is a single shared relay/phone line also used
    by pomtrader's own trade-approval "reply YES" flow -- adding a second,
    competing reply-driven flow on the same line would make an inbound
    "Yes" ambiguous between "approve this trade" and "restart this
    process", so restarting is a deliberate, separate step taken on the
    page this links to, not a text reply.)

    Also emails PROCMON_NOTIFY_EMAIL (via emailer.py) with the same
    content. The two channels are attempted independently -- the email
    exists precisely for the case where the text can't go out because
    photon-notif is itself among the crashed processes, so a failure in
    one must never suppress the other.

    Called from the reconciliation loop (a background thread, not a
    Flask request), which is why the link is built via app_context()
    rather than relying on an ambient request. Silently does nothing if
    notifications aren't configured -- this must never be the reason
    the reconcile loop itself breaks, so any failure is only logged.
    """
    link = _process_page_link(process.project_id)
    text = f"\U0001F534 {process.name} ({project_name}) crashed.\n\nRestart it here: {link}"

    if PROCMON_NOTIFY_PHONE and photon_client.PHOTON_SHARED_SECRET:
        try:
            photon_client.send_imessage(PROCMON_NOTIFY_PHONE, text)
        except Exception as e:
            logger.error("Failed to send crash text for %s: %s", process.name, e)

    if emailer.PROCMON_NOTIFY_EMAIL:
        try:
            emailer.send_email(
                emailer.PROCMON_NOTIFY_EMAIL,
                f"[procmon] {process.name} ({project_name}) crashed",
                text,
            )
        except Exception as e:
            logger.error("Failed to send crash email for %s: %s", process.name, e)


# ---------------------------------------------------------------------------
# PWA support -- sw.js MUST be served from the root path, not /static/sw.js,
# for its scope to cover the whole app rather than just the static folder.
# ---------------------------------------------------------------------------

@app.route("/sw.js")
def service_worker():
    response = send_from_directory(app.static_folder, "sw.js")
    response.headers["Content-Type"] = "application/javascript"
    return response


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if check_password(request.form.get("password", "")):
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="Incorrect password.")
    return render_template("login.html", error=None)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Dashboard / projects
# ---------------------------------------------------------------------------

def _process_status(p: Process) -> str:
    return pm.compute_process_status(p.run_mode, p.desired_state, p.pid, p.process_start_time, p.last_exit_code)


def _usage_24h(session_db, process_ids: list[str]) -> dict:
    """log/db bytes generated per process over the last 24h, for the
    at-a-glance line on each process card -- the full leaderboard and
    per-process history chart live on the dedicated /usage page."""
    if not process_ids:
        return {}
    cutoff = _now() - timedelta(hours=24)
    rows = (
        session_db.query(
            UsageSample.process_id,
            func.sum(UsageSample.log_bytes_delta),
            func.sum(UsageSample.db_bytes_delta),
        )
        .filter(UsageSample.process_id.in_(process_ids), UsageSample.sampled_at >= cutoff)
        .group_by(UsageSample.process_id)
        .all()
    )
    return {row[0]: (row[1] or 0, row[2] or 0) for row in rows}


def _project_view_data(session_db, project: Project) -> dict:
    process_rows = []
    statuses = []
    usage = _usage_24h(session_db, [p.id for p in project.processes])
    for p in project.processes:
        status = _process_status(p)
        statuses.append(status)
        log_bytes_24h, db_bytes_24h = usage.get(p.id, (0, 0))
        process_rows.append({
            "id": p.id, "name": p.name, "command": p.command,
            "working_dir": p.working_dir, "autostart": p.autostart,
            "status": status, "desired_state": p.desired_state,
            "run_mode": p.run_mode, "schedule_cron": p.schedule_cron,
            "db_tables": ", ".join(p.db_tables or []),
            "last_exit_code": p.last_exit_code,
            "last_started_at": p.last_started_at.isoformat() if p.last_started_at else None,
            "last_stopped_at": p.last_stopped_at.isoformat() if p.last_stopped_at else None,
            "awaiting_restart_confirmation": p.awaiting_restart_confirmation,
            "log_bytes_24h": _format_bytes(log_bytes_24h),
            "db_bytes_24h": _format_bytes(db_bytes_24h) if p.db_tables else None,
        })
    return {
        "id": project.id, "name": project.name,
        "status": pm.compute_project_status(statuses),
        "processes": process_rows,
    }


@app.route("/")
@login_required
def dashboard():
    with get_session() as db:
        projects = db.query(Project).order_by(Project.name).all()
        project_data = [_project_view_data(db, p) for p in projects]
    return render_template("dashboard.html", projects=project_data)


@app.route("/api/status")
@login_required
def api_status():
    with get_session() as db:
        projects = db.query(Project).order_by(Project.name).all()
        return jsonify([_project_view_data(db, p) for p in projects])


@app.route("/projects", methods=["POST"])
@login_required
def create_project():
    name = request.form.get("name", "").strip()
    if not name:
        return "Project name is required", 400
    with get_session() as db:
        existing = db.query(Project).filter_by(name=name).one_or_none()
        if existing:
            return f"A project named {name!r} already exists", 400
        db.add(Project(name=name))
    return redirect(url_for("dashboard"))


@app.route("/projects/<project_id>", methods=["GET"])
@login_required
def project_detail(project_id):
    with get_session() as db:
        project = db.query(Project).filter_by(id=project_id).one_or_none()
        if project is None:
            return "No such project", 404
        data = _project_view_data(db, project)
    return render_template("project.html", project=data)


@app.route("/projects/<project_id>/delete", methods=["POST"])
@login_required
def delete_project(project_id):
    with get_session() as db:
        project = db.query(Project).filter_by(id=project_id).one_or_none()
        if project is None:
            return "No such project", 404
        for p in project.processes:
            if _process_status(p) == "green":
                return (f"Process {p.name!r} in this project is still running -- stop it before "
                        f"deleting the project, so it doesn't become an orphaned, unmanaged process."), 400
            _unregister_scheduled_job(p.id)
        db.delete(project)
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------

@app.route("/projects/<project_id>/processes", methods=["POST"])
@login_required
def create_process(project_id):
    name = request.form.get("name", "").strip()
    command = request.form.get("command", "").strip()
    working_dir = request.form.get("working_dir", "").strip() or None
    run_mode = request.form.get("run_mode", "perpetual")
    schedule_cron = request.form.get("schedule_cron", "").strip() or None
    autostart = request.form.get("autostart") == "on"
    db_tables = _parse_db_tables(request.form.get("db_tables", ""))

    if not name or not command:
        return "Both name and command are required", 400
    if run_mode not in ("perpetual", "scheduled"):
        return f"Unrecognized run_mode: {run_mode!r}", 400
    if run_mode == "scheduled":
        if not schedule_cron:
            return "schedule_cron is required when run_mode is 'scheduled'", 400
        try:
            CronTrigger.from_crontab(schedule_cron)  # validated via APScheduler's own parser,
                                                        # not hand-rolled cron parsing -- raises a
                                                        # real, specific error for a malformed
                                                        # expression rather than silently accepting it
        except Exception as e:
            return f"Invalid cron expression {schedule_cron!r}: {e}", 400

    log_dir = os.environ.get("PROCMON_LOG_DIR", "/tmp/procmon_logs")
    with get_session() as db:
        project = db.query(Project).filter_by(id=project_id).one_or_none()
        if project is None:
            return "No such project", 404
        existing = db.query(Process).filter_by(project_id=project_id, name=name).one_or_none()
        if existing:
            return f"A process named {name!r} already exists in this project", 400

        process = Process(
            project_id=project_id, name=name, command=command, working_dir=working_dir,
            autostart=autostart, desired_state="stopped",
            run_mode=run_mode, schedule_cron=schedule_cron, db_tables=db_tables,
        )
        db.add(process)
        db.flush()
        process.log_path = os.path.join(log_dir, f"{project.name}__{process.name}__{process.id}.log")
        process_id_created = process.id

        if run_mode == "scheduled":
            _register_scheduled_job(process_id_created, schedule_cron)

    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/processes/<process_id>/edit", methods=["POST"])
@login_required
def edit_process_route(process_id):
    """
    Editable: name, command, working_dir, and either autostart
    (perpetual) or schedule_cron (scheduled) depending on the process's
    own run_mode -- run_mode itself is NOT editable here, deliberately
    a smaller scope than a full "convert between run modes" feature,
    which would need to handle stopping an active perpetual instance,
    registering/unregistering the APScheduler job, etc. Delete and
    recreate covers that rarer case; this covers the common one
    (tweaking a command, fixing a typo'd cron expression) without that
    added complexity.

    log_path is deliberately left unchanged even if the name changes --
    it's stored explicitly, not re-derived from the name, so a rename
    doesn't lose or split existing log history across two files.
    """
    name = request.form.get("name", "").strip()
    command = request.form.get("command", "").strip()
    working_dir = request.form.get("working_dir", "").strip() or None
    autostart = request.form.get("autostart") == "on"
    schedule_cron = request.form.get("schedule_cron", "").strip() or None
    db_tables = _parse_db_tables(request.form.get("db_tables", ""))

    if not name or not command:
        return "Both name and command are required", 400

    with get_session() as db:
        process = db.query(Process).filter_by(id=process_id).one_or_none()
        if process is None:
            return "No such process", 404

        existing = (
            db.query(Process)
            .filter_by(project_id=process.project_id, name=name)
            .filter(Process.id != process_id)
            .one_or_none()
        )
        if existing:
            return f"A process named {name!r} already exists in this project", 400

        if process.run_mode == "scheduled":
            if not schedule_cron:
                return "schedule_cron is required for a scheduled process", 400
            try:
                CronTrigger.from_crontab(schedule_cron)
            except Exception as e:
                return f"Invalid cron expression {schedule_cron!r}: {e}", 400

        process.name = name
        process.command = command
        process.working_dir = working_dir
        process.db_tables = db_tables
        process.auto_restart_abandoned = False  # an edit is exactly the "fixed and started manually"
                                                    # human intervention abandonment waits for -- the
                                                    # corrected command deserves a fresh crash budget
        if process.run_mode == "perpetual":
            process.autostart = autostart
        else:
            process.schedule_cron = schedule_cron
            _register_scheduled_job(process.id, schedule_cron)  # re-register with the new schedule

        project_id = process.project_id
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/processes/<process_id>/start", methods=["POST"])
@login_required
def start_process_route(process_id):
    with get_session() as db:
        process = db.query(Process).filter_by(id=process_id).one_or_none()
        if process is None:
            return "No such process", 404
        if process.run_mode == "scheduled":
            return "Use /trigger to run a scheduled process on demand, not /start.", 400

        if _process_status(process) == "green":
            return redirect(url_for("project_detail", project_id=process.project_id))

        killed = pm.kill_duplicate_processes(process.id, exclude_pid=process.pid)
        if killed:
            db.add(ProcessEvent(process_id=process.id, event_type="stopped",
                                 detail=f"killed {len(killed)} duplicate process(es) already running this command: {killed}"))

        try:
            pid, create_time = pm.start_process(process.command, process.working_dir, process.env_vars or {}, process.log_path, process.id)
        except Exception as e:
            db.add(ProcessEvent(process_id=process.id, event_type="restart_failed", detail=str(e)))
            return redirect(url_for("project_detail", project_id=process.project_id))

        process.pid = pid
        process.process_start_time = create_time
        process.desired_state = "running"
        process.last_started_at = _now()
        process.awaiting_restart_confirmation = False
        process.auto_restart_abandoned = False  # a manual start IS the human intervention the
                                                    # durable crash-loop abandonment waits for
        db.add(ProcessEvent(process_id=process.id, event_type="started", detail=f"pid={pid}"))
        project_id = process.project_id
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/processes/<process_id>/trigger", methods=["POST"])
@login_required
def trigger_scheduled_process_route(process_id):
    """
    Runs a scheduled process on demand, right now -- independent of its
    own cron schedule, which keeps firing normally regardless. Reuses
    _run_scheduled_process() directly (the exact same function
    APScheduler itself calls), just invoked from a manual click instead
    of a cron trigger, so both paths share identical logic for how a
    run starts, updates status mid-execution, and records its outcome.

    Runs in a background thread deliberately -- _run_scheduled_process
    blocks until the command actually exits (via run_one_shot's own
    race-free wait), which could take a while depending on the command.
    The HTTP request returns immediately with a "triggered" redirect
    rather than holding the connection open for however long the job
    takes to finish.
    """
    with get_session() as db:
        process = db.query(Process).filter_by(id=process_id).one_or_none()
        if process is None:
            return "No such process", 404
        if process.run_mode != "scheduled":
            return "Only scheduled processes can be triggered on demand -- use start/stop/restart for perpetual ones.", 400
        if pm.is_actually_running(process.pid, process.process_start_time):
            return "This job is already running -- wait for it to finish, or stop it first.", 400
        project_id = process.project_id

    threading.Thread(target=_run_scheduled_process, args=[process_id], daemon=True).start()
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/processes/<process_id>/stop-scheduled", methods=["POST"])
@login_required
def stop_scheduled_process_route(process_id):
    """
    Cancels a scheduled process's currently in-progress run (whether it
    was triggered manually or fired by its own cron schedule) -- not
    meaningful, and not offered, for a scheduled job that's currently
    idle between runs.

    Calls stop_process with reap=False -- confirmed directly (not just
    reasoned about) that reaping here as well as in the ALREADY-running
    _run_scheduled_process background thread's own run_one_shot()/
    process.wait() call creates a genuine race for which one consumes
    the real OS-level exit status first. The loser doesn't error
    loudly -- it silently gets a stale/default exit code instead of the
    real, signal-based one, which is a much worse failure mode than a
    crash would be. Letting only the original thread reap (via
    reap=False here) means that thread's own, single, already-correct
    DB update is the only one that happens, with no risk of either a
    corrupted exit code or two code paths racing to update the same
    row.
    """
    with get_session() as db:
        process = db.query(Process).filter_by(id=process_id).one_or_none()
        if process is None:
            return "No such process", 404
        if process.run_mode != "scheduled":
            return "Use the regular stop for perpetual processes.", 400
        pid, create_time = process.pid, process.process_start_time
        project_id = process.project_id

    if pid is not None:
        pm.stop_process(pid, create_time, reap=False)
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/processes/<process_id>/stop", methods=["POST"])
@login_required
def stop_process_route(process_id):
    with get_session() as db:
        process = db.query(Process).filter_by(id=process_id).one_or_none()
        if process is None:
            return "No such process", 404

        process.desired_state = "stopped"  # set BEFORE stopping, so a crash-detection race during
                                              # the stop itself doesn't get logged as an unexpected crash
        stopped_cleanly = pm.stop_process(process.pid, process.process_start_time)
        process.last_stopped_at = _now()
        process.awaiting_restart_confirmation = False  # a deliberate stop resolves any outstanding
                                                            # crash prompt from before
        if stopped_cleanly:
            process.pid = None
            process.process_start_time = None
        event_detail = "stopped cleanly" if stopped_cleanly else "did not confirm stopped -- may still be running, check manually"
        db.add(ProcessEvent(process_id=process.id, event_type="stopped", detail=event_detail))
        project_id = process.project_id
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/processes/<process_id>/restart", methods=["POST"])
@login_required
def restart_process_route(process_id):
    stop_process_route(process_id)  # reuses the same logic/event logging, discards its redirect
    return start_process_route(process_id)


@app.route("/processes/<process_id>/delete", methods=["POST"])
@login_required
def delete_process_route(process_id):
    with get_session() as db:
        process = db.query(Process).filter_by(id=process_id).one_or_none()
        if process is None:
            return "No such process", 404
        if _process_status(process) == "green":
            return "This process is still running -- stop it before deleting.", 400
        project_id = process.project_id
        _unregister_scheduled_job(process.id)
        db.query(ProcessEvent).filter_by(process_id=process.id).delete()
        db.delete(process)
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/processes/<process_id>/logs")
@login_required
def process_logs(process_id):
    with get_session() as db:
        process = db.query(Process).filter_by(id=process_id).one_or_none()
        if process is None:
            return jsonify({"error": "no such process"}), 404
        log_path = process.log_path
    lines = int(request.args.get("lines", 200))
    return jsonify({"log": pm.tail_log(log_path, lines=lines) if log_path else "(no log path set)"})


# ---------------------------------------------------------------------------
# Usage / data-attribution tracker
# ---------------------------------------------------------------------------

_USAGE_WINDOWS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}


@app.route("/usage")
@login_required
def usage_dashboard():
    return render_template("usage.html")


@app.route("/api/usage-summary")
@login_required
def api_usage_summary():
    """
    Per-process totals across three fixed windows in one query -- a
    conditional SUM per window rather than three separate queries, since
    all three read the exact same rows (just narrower cutoffs nested
    inside each other) and Postgres only has to scan the table once this
    way. Sorted by 24h total desc -- "what's been intensive lately" is
    the default question this page answers; the 7d/30d columns are there
    for context (a one-off spike vs. a sustained pattern), not the
    primary sort.
    """
    now = _now()
    cutoffs = {window: now - delta for window, delta in _USAGE_WINDOWS.items()}
    with get_session() as db:
        rows = db.execute(text("""
            SELECT
                s.process_id,
                p.name AS process_name,
                proj.name AS project_name,
                p.db_tables,
                SUM(CASE WHEN s.sampled_at >= :c24h THEN s.log_bytes_delta ELSE 0 END) AS log_24h,
                SUM(CASE WHEN s.sampled_at >= :c24h THEN s.db_bytes_delta ELSE 0 END) AS db_24h,
                SUM(CASE WHEN s.sampled_at >= :c7d THEN s.log_bytes_delta ELSE 0 END) AS log_7d,
                SUM(CASE WHEN s.sampled_at >= :c7d THEN s.db_bytes_delta ELSE 0 END) AS db_7d,
                SUM(CASE WHEN s.sampled_at >= :c30d THEN s.log_bytes_delta ELSE 0 END) AS log_30d,
                SUM(CASE WHEN s.sampled_at >= :c30d THEN s.db_bytes_delta ELSE 0 END) AS db_30d
            FROM procmon.usage_samples s
            JOIN procmon.processes p ON p.id = s.process_id
            JOIN procmon.projects proj ON proj.id = p.project_id
            WHERE s.sampled_at >= :c30d
            GROUP BY s.process_id, p.name, proj.name, p.db_tables
            ORDER BY SUM(CASE WHEN s.sampled_at >= :c24h THEN s.log_bytes_delta + s.db_bytes_delta ELSE 0 END) DESC
        """), {"c24h": cutoffs["24h"], "c7d": cutoffs["7d"], "c30d": cutoffs["30d"]}).mappings().all()

    results = []
    for row in rows:
        tagged = bool(row["db_tables"])
        results.append({
            "process_id": row["process_id"], "process_name": row["process_name"],
            "project_name": row["project_name"], "db_tagged": tagged,
            "windows": {
                "24h": {"log_bytes": int(row["log_24h"]), "db_bytes": int(row["db_24h"]) if tagged else None},
                "7d": {"log_bytes": int(row["log_7d"]), "db_bytes": int(row["db_7d"]) if tagged else None},
                "30d": {"log_bytes": int(row["log_30d"]), "db_bytes": int(row["db_30d"]) if tagged else None},
            },
        })
    return jsonify(results)


@app.route("/processes/<process_id>/usage-series")
@login_required
def api_process_usage_series(process_id):
    """Raw (5-minute-resolution) samples for one process's history chart
    on the /usage page -- 'hours' controls how far back, default 7 days."""
    hours = int(request.args.get("hours", 24 * 7))
    cutoff = _now() - timedelta(hours=hours)
    with get_session() as db:
        process = db.query(Process).filter_by(id=process_id).one_or_none()
        if process is None:
            return jsonify({"error": "no such process"}), 404
        samples = (
            db.query(UsageSample)
            .filter(UsageSample.process_id == process_id, UsageSample.sampled_at >= cutoff)
            .order_by(UsageSample.sampled_at.asc())
            .all()
        )
        return jsonify({
            "process_name": process.name,
            "db_tagged": bool(process.db_tables),
            "samples": [{
                "sampled_at": s.sampled_at.isoformat(),
                "log_bytes_delta": s.log_bytes_delta,
                "db_bytes_delta": s.db_bytes_delta,
            } for s in samples],
        })


def _now():
    return datetime.utcnow()


def _parse_db_tables(raw: str) -> list[str]:
    """Comma-separated "schema.table" list from a plain text form field
    into the list usage_tracker.py expects -- trims whitespace and drops
    empty entries (e.g. from a trailing comma), doesn't otherwise
    validate the table names here: an invalid/nonexistent one is caught
    per-table, harmlessly, at sample time (see usage_tracker._db_tables_size)."""
    return [t.strip() for t in raw.split(",") if t.strip()]


def _format_bytes(n) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if unit == "B":
            if n < 1024:
                return f"{int(n)}B"
        elif n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# ---------------------------------------------------------------------------
# Scheduled processes (run_mode="scheduled") -- APScheduler drives these
# ---------------------------------------------------------------------------

def _job_id(process_id: str) -> str:
    return f"process-{process_id}"


def _register_scheduled_job(process_id: str, schedule_cron: str) -> None:
    scheduler.add_job(
        _run_scheduled_process, trigger=CronTrigger.from_crontab(schedule_cron),
        args=[process_id], id=_job_id(process_id), replace_existing=True,
    )


def _unregister_scheduled_job(process_id: str) -> None:
    try:
        scheduler.remove_job(_job_id(process_id))
    except Exception:
        pass  # fine if it was never registered (e.g. deleting a perpetual process)


def _run_scheduled_process(process_id: str) -> None:
    """
    THE job APScheduler actually calls at each scheduled time. Reads the
    process's own current command/working_dir/env fresh from the DB
    (not captured at registration time), so an edited command takes
    effect on the very next scheduled run without needing to
    re-register the job.
    """
    with get_session() as db:
        process = db.query(Process).filter_by(id=process_id).one_or_none()
        if process is None or process.run_mode != "scheduled":
            return  # deleted or changed run_mode since this job was registered
        command, working_dir, env_vars, log_path = process.command, process.working_dir, process.env_vars or {}, process.log_path
        process.desired_state = "running"  # for the DURATION of this run only -- lets status
                                              # correctly show green while a longer scheduled job
                                              # is actively executing, not just after it finishes
        process.last_started_at = _now()
        db.add(ProcessEvent(process_id=process.id, event_type="started", detail="scheduled run"))

    def _on_started(pid, create_time):
        with get_session() as db:
            process = db.query(Process).filter_by(id=process_id).one_or_none()
            if process:
                process.pid = pid
                process.process_start_time = create_time

    exit_code = pm.run_one_shot(command, working_dir, env_vars, log_path, on_started=_on_started)

    with get_session() as db:
        process = db.query(Process).filter_by(id=process_id).one_or_none()
        if process is None:
            return
        process.desired_state = "stopped"
        process.pid = None
        process.process_start_time = None
        process.last_exit_code = exit_code
        process.last_stopped_at = _now()
        event_type = "stopped" if exit_code == 0 else "crashed"
        db.add(ProcessEvent(process_id=process.id, event_type=event_type, detail=f"scheduled run exited {exit_code}"))


# ---------------------------------------------------------------------------
# Startup / reconciliation
# ---------------------------------------------------------------------------

def _autostart_processes():
    """Only applies to run_mode="perpetual" -- a scheduled process's own
    cron schedule IS its "autostart" equivalent, registered separately
    in create_app() below, not gated by the autostart flag at all."""
    with get_session() as db:
        to_start = db.query(Process).filter_by(autostart=True, run_mode="perpetual").all()
        for process in to_start:
            if _process_status(process) == "green":
                continue
            # ADOPT before spawning: kill_duplicate_processes below only
            # matches procmon's own marker-wrapped instances -- a live
            # instance started OUTSIDE procmon (plain argv) survives it, and
            # blindly starting a fresh copy alongside it is exactly what
            # ignited the 8/25 storm: an untracked emon server held port
            # 8444, autostart spawned a duplicate into the conflict, and the
            # replacement died on bind every 25 seconds for ten hours.
            match = pm.find_running_instance(process.command, exclude_pid=process.pid)
            if match is not None:
                found_pid, found_create_time = match
                process.pid = found_pid
                process.process_start_time = found_create_time
                process.desired_state = "running"
                process.awaiting_restart_confirmation = False
                process.auto_restart_abandoned = False
                if process.last_started_at is None:
                    process.last_started_at = _now()
                db.add(ProcessEvent(process_id=process.id, event_type="started",
                                     detail=f"autostart: adopted already-live instance, pid={found_pid}"))
                continue
            # procmon itself restarting forgets its in-memory state, but doesn't touch
            # already-running child processes (they're their own session group) -- so an
            # old instance from the PREVIOUS procmon run can still be alive here even
            # though the DB no longer shows it as green. Clear that duplicate first.
            killed = pm.kill_duplicate_processes(process.id, exclude_pid=process.pid)
            if killed:
                db.add(ProcessEvent(process_id=process.id, event_type="stopped",
                                     detail=f"killed {len(killed)} duplicate process(es) already running this command: {killed}"))
            try:
                pid, create_time = pm.start_process(process.command, process.working_dir, process.env_vars or {}, process.log_path, process.id)
                process.pid = pid
                process.process_start_time = create_time
                process.desired_state = "running"
                process.last_started_at = _now()
                db.add(ProcessEvent(process_id=process.id, event_type="started", detail=f"autostart, pid={pid}"))
            except Exception as e:
                db.add(ProcessEvent(process_id=process.id, event_type="restart_failed", detail=f"autostart failed: {e}"))


def _register_all_scheduled_jobs():
    with get_session() as db:
        scheduled = db.query(Process).filter_by(run_mode="scheduled").all()
        for process in scheduled:
            if process.schedule_cron:
                _register_scheduled_job(process.id, process.schedule_cron)


def _recent_crash_count(db, process_id: str) -> int:
    cutoff = _now() - timedelta(minutes=CRASH_LOOP_WINDOW_MINUTES)
    return (
        db.query(ProcessEvent)
        .filter(ProcessEvent.process_id == process_id, ProcessEvent.event_type == "crashed",
                 ProcessEvent.created_at >= cutoff)
        .count()
    )


def _adopt_unreconciled_processes(db) -> None:
    """
    For every PERPETUAL process procmon doesn't currently believe is
    running (whether desired_state is "running" with a dead/stale pid,
    or "stopped" entirely), checks whether it's actually alive anyway
    -- started manually outside procmon, or left running from before
    procmon itself last restarted (a process's own OS session survives
    that; only procmon's in-memory/DB-row tracking of it doesn't). If
    so, ADOPTS it: records its real pid/create_time and marks it
    running, rather than either showing it incorrectly red or, worse,
    spawning a genuine duplicate the next time someone hits Start.

    Deliberately runs BEFORE the crash-detection pass below, in the
    same reconcile tick -- a process this adopts is, by definition, not
    actually crashed, so it must never reach that pass and get logged
    as one.
    """
    for process in db.query(Process).filter_by(run_mode="perpetual").all():
        if pm.is_actually_running(process.pid, process.process_start_time):
            continue
        match = pm.find_running_instance(process.command, exclude_pid=process.pid)
        if match is None:
            continue
        found_pid, found_create_time = match
        process.pid = found_pid
        process.process_start_time = found_create_time
        process.desired_state = "running"
        process.awaiting_restart_confirmation = False
        process.auto_restart_abandoned = False  # it's verifiably alive -- a prior crash-loop
                                                    # abandonment no longer describes reality
        if process.last_started_at is None:
            process.last_started_at = _now()
        db.add(ProcessEvent(
            process_id=process.id, event_type="started",
            detail=f"adopted -- found already running outside procmon's own tracking, pid={found_pid}",
        ))


def _reconcile_loop():
    """
    Runs forever in a background thread. For every PERPETUAL process
    whose desired_state is "running", confirms it's actually alive --
    if not, that's a real, unexpected crash. Logs it, and then actually
    attempts to restart it (the real fix for "perpetually stay up" --
    previously this only logged the crash and stopped there).

    Before any of that, _adopt_unreconciled_processes() gets first look
    at every perpetual process each tick -- see its own docstring.

    CRASH-LOOP BACKOFF, a deliberate, real safety mechanism: if a
    process has crashed CRASH_LOOP_THRESHOLD times within the last
    CRASH_LOOP_WINDOW_MINUTES, auto-restart is skipped and a single
    "restart_abandoned" event is logged instead -- an unthrottled
    auto-restart loop against a genuinely broken command would
    otherwise relaunch it every RECONCILE_INTERVAL_SECONDS forever,
    spamming both the log file and the process_events table
    indefinitely. desired_state stays "running" even when abandoned --
    this really IS still a problem needing attention (status correctly
    stays red), not something to silently mark as intentionally
    stopped. Only run_mode="perpetual" processes are considered here --
    scheduled processes are governed entirely by APScheduler's own
    triggering, not this loop.
    """
    while True:
        try:
            with get_session() as db:
                _adopt_unreconciled_processes(db)

                # Capped log rotation for every process's captured output --
                # a real 1.3 GB unrotated log helped push the disk to 98%,
                # which would eventually take down Postgres and every
                # monitored app at once. Cheap size check per tick.
                for process in db.query(Process).all():
                    if process.log_path:
                        pm.rotate_log_if_needed(process.log_path)

                running_desired = db.query(Process).filter_by(desired_state="running", run_mode="perpetual").all()
                for process in running_desired:
                    if pm.is_actually_running(process.pid, process.process_start_time):
                        continue

                    # DURABLE abandonment: once the crash-loop backstop has
                    # tripped, this process gets no more "crashed" events and
                    # no more restart attempts until a human intervenes
                    # (manual start / edit) or it's adopted verifiably alive.
                    # The old event-window check silently expired after 5
                    # minutes, so the 8/25 port-conflict storm retried all
                    # night and logged 1,400+ "crashed" rows for what was one
                    # ongoing condition. Status stays red the whole time --
                    # abandoned is still a problem, just not a spammable one.
                    if process.auto_restart_abandoned:
                        continue

                    if not process.awaiting_restart_confirmation:
                        # Both the event row and the text fire once per real
                        # crash-cycle: a successful restart clears the flag, so
                        # a genuine crash->restart->crash sequence still logs
                        # each real death, while "still dead 15s later" no
                        # longer re-logs the same death every tick forever.
                        db.add(ProcessEvent(
                            process_id=process.id, event_type="crashed",
                            detail=f"detected during routine health check -- pid {process.pid} no longer running",
                        ))
                        process.awaiting_restart_confirmation = True
                        _notify_crash(process, process.project.name)

                    if _recent_crash_count(db, process.id) >= CRASH_LOOP_THRESHOLD:
                        process.auto_restart_abandoned = True
                        db.add(ProcessEvent(
                            process_id=process.id, event_type="restart_abandoned",
                            detail=f"{CRASH_LOOP_THRESHOLD}+ crashes within {CRASH_LOOP_WINDOW_MINUTES} minutes -- "
                                   f"not auto-restarting further until this is fixed and started manually.",
                        ))
                        continue  # desired_state stays "running" -- correctly stays red, not silently "fine"

                    killed = pm.kill_duplicate_processes(process.id, exclude_pid=process.pid)
                    if killed:
                        db.add(ProcessEvent(process_id=process.id, event_type="stopped",
                                             detail=f"killed {len(killed)} duplicate process(es) already running this command: {killed}"))
                    try:
                        pid, create_time = pm.start_process(process.command, process.working_dir, process.env_vars or {}, process.log_path, process.id)
                        process.pid = pid
                        process.process_start_time = create_time
                        process.last_started_at = _now()
                        process.awaiting_restart_confirmation = False
                        db.add(ProcessEvent(process_id=process.id, event_type="started", detail=f"auto-restart after crash, pid={pid}"))
                    except Exception as e:
                        # The restart may have failed BECAUSE a live instance
                        # already holds the process's resources (the 8/25
                        # storm: an untracked orphan owned port 8444, so every
                        # replacement died on bind for ten hours). Before
                        # recording a plain failure, check for a live instance
                        # of this exact command and ADOPT it instead of
                        # fighting it.
                        match = pm.find_running_instance(process.command, exclude_pid=process.pid)
                        if match is not None:
                            found_pid, found_create_time = match
                            process.pid = found_pid
                            process.process_start_time = found_create_time
                            process.awaiting_restart_confirmation = False
                            db.add(ProcessEvent(
                                process_id=process.id, event_type="started",
                                detail=f"restart failed ({e}) but a live instance of this command was found and adopted instead, pid={found_pid}",
                            ))
                        else:
                            db.add(ProcessEvent(process_id=process.id, event_type="restart_failed", detail=f"auto-restart failed: {e}"))
        except Exception:
            # never crash the app over a reconcile failure -- but never hide
            # it either: a silently-swallowed recurring exception here means
            # NO crash detection at all (a real, observed outage of this
            # loop went unnoticed for hours behind a bare `pass`).
            logger.exception("reconcile tick failed")
        time.sleep(RECONCILE_INTERVAL_SECONDS)


def create_app():
    init_db()
    _autostart_processes()
    _register_all_scheduled_jobs()
    scheduler.start()
    threading.Thread(target=_reconcile_loop, daemon=True).start()
    threading.Thread(target=usage_tracker.sample_loop, daemon=True).start()
    return app


if __name__ == "__main__":
    create_app()
    app.run(host="0.0.0.0", port=int(os.environ.get("PROCMON_PORT", 8600)), debug=False)