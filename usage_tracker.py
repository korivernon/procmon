#!/usr/bin/python3
# ----------------------------------------------------------------------------
# Module     : procmon/usage_tracker.py
# Description: Background sampler that periodically measures how much
#              data each managed process is generating, so an
#              unusually intensive process can be spotted over time
#              rather than only noticed once disk fills up.
#
#              Two independently-measured signals per process, see
#              models.UsageSample for the storage shape:
#
#              1) log bytes: growth of the process's own captured
#                 stdout/stderr log file (Process.log_path) since the
#                 previous sample. Direct and unambiguous --
#                 os.path.getsize() on a file this tool itself owns.
#
#              2) db bytes: growth of whatever Postgres tables the
#                 process has been explicitly TAGGED as owning (see
#                 Process.db_tables). This is deliberately manual, not
#                 auto-detected -- there is no per-process disk I/O
#                 signal available here at all: psutil.Process.
#                 io_counters() is unimplemented on macOS, and even on a
#                 platform where it exists, Postgres writes physically
#                 happen in the SERVER's own backend process, never in
#                 the client that merely issued the query over its
#                 socket -- so no per-PID OS-level measurement could
#                 ever correctly attribute DB growth to a process, on
#                 any platform. Tagging is the only honest option; an
#                 untagged process simply has no DB signal rather than a
#                 fabricated one.
#
#              Each sample stores both the raw size at that moment
#              (*_total) and the growth since the prior sample
#              (*_delta) -- delta is what "how much data is this
#              generating" actually asks; total is kept alongside so a
#              chart can also show absolute size, and so delta can be
#              computed for the NEXT sample without a second query.
#
#              A log file can also legitimately SHRINK between samples
#              (process_manager.rotate_log_if_needed truncates it
#              in-place once it crosses its size cap), and a tagged DB
#              table can shrink too (a manual VACUUM FULL, a bulk
#              delete). Both are treated the same way: a total that's
#              lower than the previous sample can't be distinguished
#              from "we don't know exactly how many bytes were written
#              before whatever shrank it happened", so delta falls back
#              to the current total itself in that case (a deliberate,
#              documented undercount vs. a nonsensical negative one),
#              rather than trying to guess.
# ----------------------------------------------------------------------------

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text

from procmon.db import get_session
from procmon.models import Process, UsageSample

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_SECONDS = 5 * 60
RETENTION_DAYS = 90


def _log_size(log_path: Optional[str]) -> Optional[int]:
    if not log_path or not os.path.exists(log_path):
        return None
    try:
        return os.path.getsize(log_path)
    except OSError:
        return None


def _delta(total: Optional[int], previous_total: Optional[int]) -> int:
    if total is None:
        return 0
    if previous_total is None or total < previous_total:
        return total
    return total - previous_total


def _db_tables_size(db, tables: list[str]) -> Optional[int]:
    """
    Sums pg_total_relation_size() (includes indexes and TOAST, not just
    the bare heap -- the number that actually corresponds to disk usage)
    across every table this process is tagged with. Each lookup runs in
    its own SAVEPOINT: a stale/typo'd/dropped table name would otherwise
    raise and poison the rest of the CURRENT transaction for every other
    table and process still to be sampled this tick, not just its own
    lookup -- confirmed necessary, not defensive over-engineering, since
    a single Postgres error aborts the whole enclosing transaction until
    it's rolled back.

    The table name is passed as a bound parameter, not string-formatted
    into the query -- CAST(:t AS regclass) parses it as an identifier
    server-side, the same as any other bound value, so a malformed or
    hostile string just fails to resolve (caught below) rather than
    being interpreted as SQL. Written as CAST(...) rather than the more
    common "::regclass" shorthand deliberately -- confirmed directly
    that SQLAlchemy's text() does NOT substitute a bind param
    immediately followed by "::" (it's left as literal ":t::regclass" in
    the query psycopg2 receives, raising a syntax error), so the
    shorthand silently breaks parameter binding here.
    """
    if not tables:
        return None
    total = 0
    any_ok = False
    for raw in tables:
        table = (raw or "").strip()
        if not table:
            continue
        try:
            with db.begin_nested():
                size = db.execute(
                    text("SELECT pg_total_relation_size(CAST(:t AS regclass))"), {"t": table}
                ).scalar()
        except Exception as e:
            logger.warning("usage_tracker: could not size table %r: %s", table, e)
            continue
        if size is not None:
            total += int(size)
            any_ok = True
    return total if any_ok else None


def sample_all() -> None:
    now = datetime.utcnow()
    with get_session() as db:
        processes = db.query(Process).all()

        latest_by_process = {}
        for process in processes:
            latest_by_process[process.id] = (
                db.query(UsageSample)
                .filter_by(process_id=process.id)
                .order_by(UsageSample.sampled_at.desc())
                .first()
            )

        for process in processes:
            previous = latest_by_process[process.id]

            log_total = _log_size(process.log_path)
            log_delta = _delta(log_total, previous.log_bytes_total if previous else None)

            db_total = _db_tables_size(db, process.db_tables or [])
            db_delta = _delta(db_total, previous.db_bytes_total if previous else None)

            if log_total is None and db_total is None:
                continue  # nothing measurable for this process yet -- e.g. never started, no log
                            # file on disk, and no db_tables tagged

            db.add(UsageSample(
                process_id=process.id, sampled_at=now,
                log_bytes_total=log_total, log_bytes_delta=log_delta,
                db_bytes_total=db_total, db_bytes_delta=db_delta,
            ))

        cutoff = now - timedelta(days=RETENTION_DAYS)
        db.query(UsageSample).filter(UsageSample.sampled_at < cutoff).delete()


def sample_loop() -> None:
    """Runs forever in a background thread -- same never-crash-the-app
    shape as app.py's own _reconcile_loop: a failure here must be logged
    loudly, not swallowed silently, but must also never take down the
    thread itself, or attribution tracking just silently stops forever."""
    while True:
        try:
            sample_all()
        except Exception:
            logger.exception("usage_tracker: sample tick failed")
        time.sleep(SAMPLE_INTERVAL_SECONDS)
