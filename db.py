#!/usr/bin/python3
# ----------------------------------------------------------------------------
# Module     : procmon/db.py
# Last updated: 2026-08-21 02:15 EDT
# Description: DB setup for the process monitor. Uses the SAME Postgres
#              instance pomtrader already runs against, but a SEPARATE
#              schema ("procmon", not "pomtrader") -- this is deliberately
#              a standalone tool, not part of pomtrader itself, per the
#              explicit request, while still reusing existing
#              infrastructure rather than standing up a second database.
#
#              PROCMON_DATABASE_URL defaults to the same connection
#              details documented for pomtrader's own Postgres instance
#              this session (admin@127.0.0.1:5433/postgres) -- override
#              via env var if that's not actually right; this default is
#              a reasonable guess grounded in what's been confirmed
#              elsewhere this session, not something verified against a
#              real connection from here.
# ----------------------------------------------------------------------------

from __future__ import annotations

import os
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.environ.get(
    "PROCMON_DATABASE_URL",
    "postgresql+psycopg2://admin@127.0.0.1:5433/postgres",
)

_engine = create_engine(DATABASE_URL, pool_pre_ping=True)
_SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def init_db():
    """Creates the procmon schema and all tables if they don't already
    exist. Safe to call on every app startup -- idempotent.

    create_all() only creates missing TABLES, not missing columns on a
    table that already exists -- so a new column added to models.py
    after this schema was first created (e.g. awaiting_restart_
    confirmation) needs its own explicit, idempotent ALTER TABLE here as
    well. Not a real migration tool, just enough to keep an
    already-populated table in sync with models.py without a manual
    psql step."""
    with _engine.connect() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS procmon"))
        conn.commit()
    from procmon.models import Base
    Base.metadata.create_all(_engine)
    with _engine.connect() as conn:
        conn.execute(text(
            "ALTER TABLE procmon.processes "
            "ADD COLUMN IF NOT EXISTS awaiting_restart_confirmation BOOLEAN DEFAULT FALSE"
        ))
        conn.execute(text(
            "ALTER TABLE procmon.processes "
            "ADD COLUMN IF NOT EXISTS db_tables JSONB DEFAULT '[]'::jsonb"
        ))
        conn.commit()


@contextmanager
def get_session():
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
