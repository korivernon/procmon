#!/usr/bin/python3
# ----------------------------------------------------------------------------
# Module     : procmon/models.py
# Last updated: 2026-08-21 02:15 EDT
# Description: Schema for the process monitor.
#
#              desired_state vs. actual liveness are deliberately
#              separate concepts: desired_state ("running"/"stopped") is
#              what the USER asked for -- it's what makes the difference
#              between "this process is dead because it crashed" (a real
#              problem, worth an alert) and "this process is dead because
#              someone stopped it on purpose" (expected, not a problem).
#              Actual liveness is computed fresh on every status check
#              via process_manager.py, not read from a stored column --
#              a stored "is it running" boolean would go stale the
#              moment a process crashes on its own between checks.
#
#              log_path points at a file on disk, not the log CONTENT
#              itself -- storing every line of stdout/stderr for a
#              long-running process directly in Postgres doesn't scale
#              well and isn't necessary; the file is the source of
#              truth, this just points at it.
# ----------------------------------------------------------------------------

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def _uuid() -> str:
    return str(uuid.uuid4())


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = {"schema": "procmon"}

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name = Column(String, nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    processes = relationship("Process", back_populates="project", cascade="all, delete-orphan")


class Process(Base):
    __tablename__ = "processes"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_process_project_name"),
        {"schema": "procmon"},
    )

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    project_id = Column(UUID(as_uuid=False), ForeignKey("procmon.projects.id"), nullable=False)
    name = Column(String, nullable=False)
    command = Column(Text, nullable=False)  # shell command, run via subprocess with shell=True
    working_dir = Column(Text, nullable=True)
    env_vars = Column(JSONB, default=dict)  # extra env vars merged over the current environment

    autostart = Column(Boolean, default=False)  # start automatically when the monitor itself boots --
                                                    # only meaningful for run_mode="perpetual"
    log_path = Column(Text, nullable=True)

    db_tables = Column(JSONB, default=list)  # optional list of "schema.table" strings this process is
                                                 # tagged as owning -- see usage_tracker.py's module
                                                 # docstring for why this has to be an explicit, manual
                                                 # tag rather than something auto-detected: Postgres writes
                                                 # happen in the SERVER process, never in the client that
                                                 # issued the query, so there is no per-PID measurement
                                                 # that could ever attribute DB growth to a process on its
                                                 # own. Untagged (empty list) just means "no DB signal for
                                                 # this process", not "zero usage".

    run_mode = Column(String, default="perpetual")  # "perpetual" | "scheduled"
    schedule_cron = Column(String, nullable=True)  # standard 5-field cron expression, only meaningful
                                                       # when run_mode="scheduled" -- e.g. "0 9 * * *" for
                                                       # 9am daily. Parsed/validated via APScheduler's own
                                                       # CronTrigger, not hand-rolled cron parsing.

    pid = Column(Integer, nullable=True)  # last known PID -- verified live via process_manager,
                                             # not trusted blindly (see its own module docstring)
    process_start_time = Column(String, nullable=True)  # psutil's own create_time(), stored as a
                                                            # string -- see process_manager.py for why
                                                            # this matters for correctly detecting PID reuse

    desired_state = Column(String, default="stopped")  # "running" | "stopped" -- what the user asked for,
                                                           # NOT whether it's actually alive right now
    last_started_at = Column(DateTime, nullable=True)
    last_stopped_at = Column(DateTime, nullable=True)
    last_exit_code = Column(Integer, nullable=True)

    awaiting_restart_confirmation = Column(Boolean, default=False)  # set when a crash notification
                                                                        # has been sent via photon-notif
                                                                        # and a "reply YES to restart" is
                                                                        # outstanding -- cleared as soon as
                                                                        # the process is next successfully
                                                                        # started, by whatever path does it
    auto_restart_abandoned = Column(Boolean, default=False)  # DURABLE crash-loop backstop: set once a
                                                                 # process hits CRASH_LOOP_THRESHOLD crashes
                                                                 # in the window, after which the reconcile
                                                                 # loop stops both restarting it AND logging
                                                                 # further "crashed" events for it. Cleared
                                                                 # only by a manual start, an edit, or the
                                                                 # process being adopted alive. Replaces the
                                                                 # old event-window check, which quietly
                                                                 # expired after 5 minutes and made the
                                                                 # "not auto-restarting further" promise
                                                                 # false -- the 8/25 storm retried (and
                                                                 # event-spammed) all night because of it.

    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="processes")


class ProcessEvent(Base):
    """
    A structured history of start/stop/crash events -- separate from the
    raw stdout/stderr log file. Lets the UI show "this crashed 3 times
    today" without parsing free-text logs.
    """
    __tablename__ = "process_events"
    __table_args__ = {"schema": "procmon"}

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    process_id = Column(UUID(as_uuid=False), ForeignKey("procmon.processes.id"), nullable=False)
    event_type = Column(String, nullable=False)  # "started" | "stopped" | "crashed" | "restart_failed"
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class UsageSample(Base):
    """
    A periodic (see usage_tracker.SAMPLE_INTERVAL_SECONDS) data-attribution
    snapshot for one process -- how many bytes its log file grew by, and
    how many bytes its tagged DB tables (Process.db_tables) grew by,
    since the previous sample. *_total is the raw size at sample time
    (lets a chart show absolute size, not just the growth rate); *_delta
    is the growth since the prior sample for this same process (what
    actually answers "how much data is this process generating" -- the
    thing being tracked over time). Either total/delta pair is nullable
    independently: a process with no log_path yet, or no db_tables
    tagged, simply has no signal for that half, not a zero.
    """
    __tablename__ = "usage_samples"
    __table_args__ = (
        Index("ix_usage_samples_process_sampled_at", "process_id", "sampled_at"),
        {"schema": "procmon"},
    )

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    process_id = Column(UUID(as_uuid=False), ForeignKey("procmon.processes.id"), nullable=False)
    sampled_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    log_bytes_total = Column(BigInteger, nullable=True)
    log_bytes_delta = Column(BigInteger, nullable=False, default=0)

    db_bytes_total = Column(BigInteger, nullable=True)
    db_bytes_delta = Column(BigInteger, nullable=False, default=0)
