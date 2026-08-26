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
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
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
