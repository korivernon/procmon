#!/usr/bin/python3
# ----------------------------------------------------------------------------
# Module     : procmon/process_manager.py
# Last updated: 2026-08-21 02:20 EDT
# Description: Actual process lifecycle management -- start, stop,
#              restart, and liveness checking.
#
#              THE CORE CORRECTNESS CONCERN, and why this file exists as
#              its own tested unit rather than being inlined into Flask
#              routes: a long-running monitor process needs to check
#              whether a PID it recorded earlier is STILL the same
#              process, not just whether SOME process with that number
#              currently exists. Operating systems reuse PIDs once a
#              process exits -- if this tool's own web server restarts
#              (a real, expected event, not an edge case) and later
#              checks a stored PID naively via just "does this PID
#              exist", a since-crashed process's PID could have been
#              reassigned by the OS to a completely unrelated process by
#              the time of the next check, producing a FALSE "still
#              running" result -- exactly the kind of silent lie a
#              process monitor must not produce. Fixed by also storing
#              and comparing the process's own start time (psutil's
#              create_time()) alongside the PID -- the combination of
#              (pid, create_time) is what's actually unique, not the PID
#              alone.
# ----------------------------------------------------------------------------

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from datetime import datetime
from typing import Optional

import psutil

logger = logging.getLogger(__name__)

GRACEFUL_STOP_TIMEOUT_SECONDS = 10


def _duplicate_marker(process_id: str) -> str:
    """
    A shell no-op prefix embedding process_id, prepended to a process's
    command before it's actually run. Exists purely so kill_duplicate_
    processes() can later find a still-running instance of THIS EXACT
    process definition precisely -- reading another process's environ()
    to tag it there instead was tried first and confirmed NOT to work
    unprivileged on macOS (returns empty even for a same-user child), so
    the marker has to live somewhere psutil CAN reliably read for any
    process -- its own argv, via cmdline().

    The leading ": <marker> ;" also has a second, required effect: it
    forces the shell to actually stay alive as a wrapper around the
    real command rather than exec-replacing itself with it (which shells
    do for a single simple command with nothing else in the script --
    confirmed directly, not assumed). Losing the wrapper would lose the
    marker along with it, since an exec-replaced process's argv becomes
    the target command's own, with no room left for the marker at all.
    """
    return f": PROCMON_ID_{process_id} ;"


def start_process(command: str, working_dir: Optional[str], env_vars: dict, log_path: str,
                   process_id: str) -> tuple[int, str]:
    """
    Spawns the given shell command, redirecting stdout+stderr to
    log_path (appended, not truncated -- restarting a process shouldn't
    silently discard its prior history). Returns (pid, create_time_str)
    -- both are needed together for correct liveness checks later, see
    module docstring.

    For PERPETUAL/long-running processes only -- treats an immediate
    exit as suspicious and raises, since a daemon quitting right away
    signals something's actually broken (missing dependency, bad
    config, etc.). For scheduled/one-shot commands, where running
    briefly and exiting cleanly is the entire point, use run_one_shot()
    instead -- it's also the only race-free way to capture a fast
    command's real exit code; see its own docstring for the confirmed
    race this function alone doesn't protect against.

    Uses shell=True deliberately -- commands are expected to be full
    shell command lines (e.g. "POMTRADER_ENV=dev python3 -m
    pomtrader.execution.scheduler"), not a pre-split argv list, matching
    how a person would naturally type this into the UI. This does mean
    a process definition IS an arbitrary shell command with all the
    trust implications that carries -- see app.py's own auth layer,
    this tool assumes whoever can reach it is already trusted to run
    arbitrary commands on this machine, the same trust level as having
    a terminal open on it.

    process_id is procmon's own DB id for this process definition --
    embedded (via _duplicate_marker) as a harmless leading no-op in the
    actual command run, purely so a LATER call to kill_duplicate_
    processes(process_id) can recognize and clean up THIS specific
    still-running instance if this process definition gets started
    again while an old instance is unexpectedly still alive (e.g.
    procmon itself restarted and lost track of it). ":" is a shell
    builtin that does nothing and produces no output, so this doesn't
    change what the command actually does or logs.
    """
    log_dir = os.path.dirname(log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    full_env = dict(os.environ)
    full_env.update(env_vars or {})

    wrapped_command = f"{_duplicate_marker(process_id)} {command}"

    log_file = open(log_path, "a")
    try:
        proc = subprocess.Popen(
            wrapped_command,
            shell=True,
            cwd=working_dir or None,
            env=full_env,
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # own process group -- lets stop_process() signal the whole
                                        # group, not just the immediate shell, so a command like
                                        # "python3 foo.py" spawned via shell=True gets properly
                                        # signalled even though the shell itself is the direct child
        )
    finally:
        log_file.close()  # the child inherited its own fd to this file; the parent's own
                              # handle isn't needed after Popen returns

    # A brief pause + liveness check catches the common "command doesn't
    # exist" / immediate-crash case right away, rather than reporting a
    # fake success and only discovering the failure on the next status
    # poll. Not a guarantee the process will stay up -- just catches the
    # most common, immediate failure mode directly.
    time.sleep(0.3)
    if proc.poll() is not None:
        raise RuntimeError(
            f"process exited immediately (exit code {proc.returncode}) -- check the command is correct. "
            f"See {log_path} for whatever output it produced before exiting."
        )

    try:
        create_time = psutil.Process(proc.pid).create_time()
    except psutil.NoSuchProcess:
        raise RuntimeError("process could not be found immediately after starting -- it likely exited "
                            "in the brief window between the poll() check above and this line")

    return proc.pid, str(create_time)


def kill_duplicate_processes(process_id: str, exclude_pid: Optional[int] = None) -> list[int]:
    """
    Finds and force-kills any OS process previously started (via
    start_process, below) for this exact process_id that's still
    running -- meant to be called right BEFORE start_process() spawns a
    new instance of it, so an old instance left running after procmon
    itself lost track of it (e.g. across a procmon restart, which
    forgets in-memory/DB-row state but doesn't touch already-running
    child processes) doesn't end up duplicated alongside the new one
    about to be spawned.

    Matching is via the marker start_process() embeds in the process's
    own argv (_duplicate_marker/cmdline()), NOT by comparing raw command
    text -- an earlier version of this function matched by command text
    and was confirmed, by direct testing, to have a real false-positive
    risk: a compound command's internal step (e.g. the "sleep 30" part
    of "sleep 30 && echo done") can produce a process whose own argv is
    indistinguishable from an unrelated simple "sleep 30" command. The
    marker's per-process_id uniqueness avoids that entirely -- this can
    only ever match a process procmon itself started for this specific
    process definition, never an unrelated one, and never one started
    outside procmon at all (which is a deliberate, safer scope: catching
    procmon's own orphaned instances, not guessing at arbitrary
    processes on the system that merely look similar).

    Returns the pids actually killed, purely for logging -- callers
    don't need to do anything else with them.
    """
    marker = _duplicate_marker(process_id)
    own_pid = os.getpid()
    killed = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        if proc.pid in (exclude_pid, own_pid):
            continue
        try:
            cmdline = proc.info["cmdline"] or []
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

        if len(cmdline) < 3 or cmdline[1] != "-c" or not cmdline[2].startswith(marker):
            continue

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            killed.append(proc.pid)
        except (ProcessLookupError, PermissionError):
            continue

    if killed:
        time.sleep(0.3)
        for pid in killed:
            try:
                if psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, psutil.NoSuchProcess):
                pass
    return killed


def is_actually_running(pid: Optional[int], expected_create_time: Optional[str]) -> bool:
    """
    True only if a process with this exact PID exists, is NOT a zombie,
    AND its own create_time matches what was recorded when we started
    it.

    Both checks are real, confirmed necessities, not defensive
    over-engineering -- verified directly against an actual subprocess:
    a terminated-but-unreaped process is a ZOMBIE, which still exists as
    a process-table entry (psutil.Process(pid) succeeds, no
    NoSuchProcess raised) even though it is no longer running anything
    at all. Checking existence alone produced a confirmed false
    "still running" positive in testing. create_time is separately
    needed to guard against PID reuse across a genuinely different
    process once the original is actually gone (see module docstring).
    A small float tolerance is used for the create_time comparison
    since psutil's own value can have tiny floating-point
    representation differences across platforms/reads of the same
    underlying kernel timestamp.
    """
    if pid is None or not expected_create_time:
        return False
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
    except psutil.NoSuchProcess:
        return False
    try:
        actual_create_time = proc.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    return abs(actual_create_time - float(expected_create_time)) < 1.0


def stop_process(pid: Optional[int], expected_create_time: Optional[str], reap: bool = True) -> bool:
    """
    Graceful SIGTERM first, escalating to SIGKILL only if it doesn't
    exit within GRACEFUL_STOP_TIMEOUT_SECONDS -- signals the whole
    process GROUP (negative pid), not just the recorded PID alone,
    since start_process() launches with start_new_session=True
    specifically so this works correctly for a shell=True command
    where the recorded PID is the shell, not necessarily the real
    long-running child.

    reap=True (the default) is correct for a perpetual process, where
    nothing else is waiting on this pid -- stop_process itself must
    reap it, or it lingers as a zombie. reap=False is REQUIRED when
    stopping a scheduled process's in-progress run: confirmed directly,
    not theorized, that a SEPARATE thread's own run_one_shot() call is
    already blocked inside its own process.wait() for this exact pid --
    reaping here too creates a genuine race for who consumes the real
    OS-level exit status first. Losing that race silently corrupts the
    exit code (observed 0 instead of the real, signal-based negative
    value in testing) rather than raising an obvious error, which made
    this a real, confirmed bug -- not a hypothetical one -- caught by
    actually running the scenario and checking the recorded exit code,
    not by reasoning about it in the abstract.

    Returns True if the process is confirmed gone by the end of this
    call, False if it's still alive despite SIGKILL (a real, if rare,
    possible outcome -- e.g. a process stuck in uninterruptible I/O
    wait -- surfaced honestly rather than claimed as a success it
    wasn't).
    """
    if not is_actually_running(pid, expected_create_time):
        return True  # already gone -- nothing to do, and this is success, not failure

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        logger.warning("stop_process(%s): permission denied sending SIGTERM", pid)
        return False

    deadline = time.time() + GRACEFUL_STOP_TIMEOUT_SECONDS
    while time.time() < deadline:
        if not is_actually_running(pid, expected_create_time):
            if reap:
                _reap_if_ours(pid)
            return True
        time.sleep(0.2)

    logger.warning("stop_process(%s): did not exit within %ss of SIGTERM, escalating to SIGKILL",
                    pid, GRACEFUL_STOP_TIMEOUT_SECONDS)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False

    time.sleep(0.5)
    still_running = is_actually_running(pid, expected_create_time)
    if not still_running and reap:
        _reap_if_ours(pid)
    return not still_running


def _reap_if_ours(pid: int) -> None:
    """
    Best-effort: if this process is the actual OS-level parent of pid
    (true within the same monitor session that spawned it), reaping it
    here avoids it lingering as a zombie until something else reaps it.
    Harmless no-op if we're not the parent (e.g. after the monitor
    itself has restarted since this process was started) -- ECHILD is
    expected and swallowed in that case, not an error worth surfacing.
    """
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


def wait_for_exit(pid: int, timeout: Optional[float] = None) -> Optional[int]:
    """
    Blocks until the process exits, returning its REAL exit code.

    KNOWN, CONFIRMED LIMITATION: for a command that exits fast enough,
    there's a genuine race between this process starting and this
    function's own first psutil lookup -- if the process has already
    exited AND been reaped by the time this runs, the real exit code is
    permanently unrecoverable (the OS discards it once reaped), and
    this returns None rather than a fabricated guess. Confirmed this
    directly, not theorized -- a plain "exit 0" command reliably hit
    this race in testing. run_one_shot() below is the actual fix for
    the scheduled/one-shot use case -- it never lets go of the original
    Popen object, so its own .wait() is race-free by construction. This
    function is kept for checking on a process this code did NOT
    itself just start (e.g. an already-running perpetual process), where
    that race doesn't apply the same way.
    """
    try:
        proc = psutil.Process(pid)
        return proc.wait(timeout=timeout)
    except psutil.NoSuchProcess:
        return None
    except psutil.TimeoutExpired:
        return None


def run_one_shot(command: str, working_dir: Optional[str], env_vars: dict, log_path: str,
                  on_started=None) -> int:
    """
    THE actual, race-free way to run a scheduled/one-shot command and
    get its real exit code -- built specifically to fix a confirmed
    race in the alternative approach (start_process() + a separate,
    later wait_for_exit(pid) call): for a fast-exiting command, the
    process can exit AND be reaped before a second, separate psutil
    lookup ever gets a chance to see it, permanently losing the real
    exit code. This function never lets go of the original Popen
    object -- its own .wait() is correct by construction regardless of
    how fast the command exits, since Popen tracks this internally
    without needing a second process-table lookup at all.

    on_started(pid, create_time_str), if given, is called immediately
    after the process starts, BEFORE waiting for it to finish -- lets a
    caller (the scheduler in app.py) record the pid so status correctly
    shows "green, mid-run" for a scheduled job that takes a while,
    without needing a second start_process()-style call that would
    reintroduce the exact race this function exists to avoid.
    """
    log_dir = os.path.dirname(log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    full_env = dict(os.environ)
    full_env.update(env_vars or {})

    log_file = open(log_path, "a")
    try:
        proc = subprocess.Popen(
            command, shell=True, cwd=working_dir or None, env=full_env,
            stdout=log_file, stderr=log_file, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        log_file.close()

    if on_started is not None:
        try:
            create_time = str(psutil.Process(proc.pid).create_time())
        except psutil.NoSuchProcess:
            create_time = "0"  # already finished by the time we could look -- fine, on_started
                                  # is only used for "currently running" status display, and
                                  # wait() below still correctly gets the real exit code regardless
        on_started(proc.pid, create_time)

    return proc.wait()


def compute_process_status(run_mode: str, desired_state: str, pid: Optional[int],
                            expected_create_time: Optional[str], last_exit_code: Optional[int]) -> str:
    """
    Two genuinely different rules depending on run_mode -- confirmed
    this distinction is necessary, not just a style preference: a
    scheduled process is EXPECTED to be not-currently-running between
    its scheduled times, which is the normal, healthy state, not a
    problem. Reusing the perpetual rule (green only while alive) would
    incorrectly show red for a scheduled job simply waiting for its
    next run.

    perpetual: unchanged from before -- green iff desired to be
    running AND actually, verifiably alive right now, red otherwise.

    scheduled: NEVER red -- confirmed explicit, per direct request. A
    scheduled job's normal, expected state is "not currently running,
    waiting for its next scheduled time" -- that's not a failure state
    the way a crashed perpetual daemon is, so it shouldn't share red's
    "something is actively wrong" meaning. green if currently mid-run
    OR the last completed run exited 0. amber otherwise -- covers both
    "last run failed" and "never run yet at all", since neither is a
    hard, confirmed problem the way a crash is: a failed scheduled run
    might just need to wait for its next attempt, and "never run yet"
    is genuinely unproven, not confirmed broken.
    """
    if run_mode == "scheduled":
        currently_running = desired_state == "running" and is_actually_running(pid, expected_create_time)
        if currently_running:
            return "green"
        return "green" if last_exit_code == 0 else "amber"

    if desired_state == "running" and is_actually_running(pid, expected_create_time):
        return "green"
    return "red"


def compute_project_status(process_statuses: list[str]) -> str:
    """
    green: every process green, OR a mix of green and amber with no red
    at all -- amber alone (a scheduled job that hasn't proven itself yet,
    or is simply idle between runs) isn't a real problem, so it
    shouldn't drag an otherwise-healthy project down to a warning
    color. red: every process red (or no processes at all -- an empty
    project has nothing green to report). amber: red is present
    alongside anything else (not ALL red) -- red is what actually
    signals a real problem here, e.g. a green process running fine
    next to a crashed one.

    An all-amber project (no green, no red at all -- e.g. every
    scheduled job present has simply never run yet) deliberately stays
    amber rather than being promoted to green: nothing here has
    actually proven itself working, so "everything's fine" would be
    overstating it. Only promoted to green when there's at least one
    confirmed-green process alongside the amber ones.
    """
    if not process_statuses:
        return "red"
    if all(s == "green" for s in process_statuses):
        return "green"
    if all(s == "red" for s in process_statuses):
        return "red"

    has_red = any(s == "red" for s in process_statuses)
    has_green = any(s == "green" for s in process_statuses)
    if not has_red and has_green:
        return "green"
    return "amber"


def tail_log(log_path: str, lines: int = 200) -> str:
    """
    Reads the last N lines of a log file efficiently -- reads from the
    end in chunks rather than loading a potentially large file entirely
    into memory just to keep its tail.
    """
    if not os.path.exists(log_path):
        return "(no log file yet)"

    chunk_size = 8192
    with open(log_path, "rb") as f:
        f.seek(0, os.SEEK_END)
        file_size = f.tell()
        blocks = []
        lines_found = 0
        position = file_size
        while position > 0 and lines_found <= lines:
            read_size = min(chunk_size, position)
            position -= read_size
            f.seek(position)
            chunk = f.read(read_size)
            blocks.append(chunk)
            lines_found += chunk.count(b"\n")
        content = b"".join(reversed(blocks))
    text = content.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])