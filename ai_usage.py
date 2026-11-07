"""AI usage across every project procmon manages, and the levers that turn it off. Fed by ai_shim inside each app."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timedelta

from sqlalchemy import text

from procmon.db import get_session
from procmon.models import AiCall, AiLever, Project

logger = logging.getLogger(__name__)

RETENTION_DAYS = 90
SEEN_WITHIN_SECONDS = 60
_STATUSES = {"ok", "error", "blocked"}

# USD per million tokens: (input, output, cache read or None for input x CACHE_READ_MULTIPLIER). Anthropic list
# prices; the 5.x rows were checked against the claude-api reference on 2026-11-07. A model not listed gets cost NULL
# and shows as unpriced rather than a guess.
PRICES = {
    "claude-haiku-4-5-20251001": (1.00, 5.00, None),
    "claude-haiku-4-5": (1.00, 5.00, None),
    "claude-haiku-5-5": (0.10, 0.50, None),
    "claude-sonnet-5": (2.00, 10.00, None),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-opus-4-8": (5.00, 25.00, None),
    "claude-opus-5": (5.00, 25.00, None),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-fable-5": (10.00, 50.00, None),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
}
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10
WEB_SEARCH_COST = 0.01

_seen = {}              # project_id -> {process name: last sync time}; in memory, refilled within one poll
_seen_lock = threading.Lock()
_last_prune = 0.0


def valid_project_id(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


def cost_of(model, input_tokens, output_tokens, cache_write, cache_read, web_searches):
    rates = PRICES.get(model)
    if rates is None:
        return None
    rate_in, rate_out, rate_read = rates
    if rate_read is None:
        rate_read = rate_in * CACHE_READ_MULTIPLIER
    return round((input_tokens * rate_in + output_tokens * rate_out + cache_write * rate_in * CACHE_WRITE_MULTIPLIER
                  + cache_read * rate_read) / 1_000_000 + web_searches * WEB_SEARCH_COST, 6)


def _int(value) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _clean(value, limit=80):
    return str(value)[:limit] if value else None


def sync(body: dict) -> dict:
    """Takes an app's usage and new categories, answers with its levers."""
    project_id = body.get("project_id")
    if not valid_project_id(project_id):
        return {"managed": False}
    process = _clean(body.get("process"))
    with get_session() as db:
        if db.query(Project.id).filter_by(id=project_id).first() is None:
            return {"managed": False}

        levers = {lever.category: lever.enabled for lever in db.query(AiLever).filter_by(project_id=project_id)}
        events = body.get("events") if isinstance(body.get("events"), list) else []
        declared = body.get("categories") if isinstance(body.get("categories"), list) else []
        names = {_clean(name) for name in declared} | {_clean(e.get("category")) for e in events if isinstance(e, dict)}
        for name in names - set(levers) - {None}:
            db.add(AiLever(project_id=project_id, category=name, enabled=True))
            levers[name] = True

        for event in events:
            if not isinstance(event, dict) or not event.get("category") or event.get("status") not in _STATUSES:
                continue
            tokens_in, tokens_out = _int(event.get("input_tokens")), _int(event.get("output_tokens"))
            cache_write, cache_read = _int(event.get("cache_write_tokens")), _int(event.get("cache_read_tokens"))
            searches = _int(event.get("web_searches"))
            model = _clean(event.get("model"))
            try:
                when = datetime.utcfromtimestamp(float(event["ts"]))
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                when = datetime.utcnow()
            db.add(AiCall(
                project_id=project_id, process_name=process, category=_clean(event["category"]),
                provider=_clean(event.get("provider")), model=model, status=event["status"],
                input_tokens=tokens_in, output_tokens=tokens_out, cache_write_tokens=cache_write,
                cache_read_tokens=cache_read, web_searches=searches, created_at=when,
                cost=cost_of(model, tokens_in, tokens_out, cache_write, cache_read, searches),
            ))
    with _seen_lock:
        _seen.setdefault(project_id, {})[process or "?"] = time.time()
    _prune_hourly()
    return {"managed": True, "enabled": levers.get("", True),
            "disabled": sorted(name for name, enabled in levers.items() if name and not enabled)}


def _prune_hourly():
    global _last_prune
    if time.time() - _last_prune < 3600:
        return
    _last_prune = time.time()
    try:
        with get_session() as db:
            cutoff = datetime.utcnow() - timedelta(days=RETENTION_DAYS)
            db.query(AiCall).filter(AiCall.created_at < cutoff).delete()
    except Exception:
        logger.exception("ai_usage: prune failed")


def set_lever(project_id: str, category: str, enabled: bool) -> None:
    with get_session() as db:
        lever = db.query(AiLever).filter_by(project_id=project_id, category=category).one_or_none()
        if lever is None:
            db.add(AiLever(project_id=project_id, category=category, enabled=enabled))
        else:
            lever.enabled = enabled


def _window_sums(where_group: str) -> str:
    columns = []
    for window in ("24h", "7d", "30d"):
        since = f"c.created_at >= :c{window}"
        tokens = "c.input_tokens + c.output_tokens + c.cache_write_tokens + c.cache_read_tokens"
        columns += [
            f"COALESCE(SUM(c.cost) FILTER (WHERE {since}), 0) AS cost_{window}",
            f"COUNT(*) FILTER (WHERE {since} AND c.status <> 'blocked') AS calls_{window}",
            f"COUNT(*) FILTER (WHERE {since} AND c.status = 'blocked') AS blocked_{window}",
            f"COUNT(*) FILTER (WHERE {since} AND c.status = 'error') AS errors_{window}",
            f"COALESCE(SUM({tokens}) FILTER (WHERE {since}), 0) AS tokens_{window}",
            f"COUNT(*) FILTER (WHERE {since} AND c.status = 'ok' AND c.cost IS NULL) AS unpriced_{window}",
        ]
    return (f"SELECT c.project_id, {where_group} {', '.join(columns)} FROM procmon.ai_calls c "
            f"WHERE c.created_at >= :c30d GROUP BY c.project_id {', c.category' if 'category' in where_group else ''}")


def _windows(row) -> dict:
    return {window: {
        "cost": float(row[f"cost_{window}"]), "calls": row[f"calls_{window}"], "blocked": row[f"blocked_{window}"],
        "errors": row[f"errors_{window}"], "tokens": int(row[f"tokens_{window}"]), "unpriced": row[f"unpriced_{window}"],
    } for window in ("24h", "7d", "30d")}


def summary() -> dict:
    now = datetime.utcnow()
    since = {"c24h": now - timedelta(hours=24), "c7d": now - timedelta(days=7), "c30d": now - timedelta(days=30)}
    with get_session() as db:
        projects = db.query(Project).order_by(Project.name).all()
        levers = db.query(AiLever).all()
        by_project = {str(r["project_id"]): r for r in db.execute(text(_window_sums("")), since).mappings()}
        by_category = {(str(r["project_id"]), r["category"]): r
                       for r in db.execute(text(_window_sums("c.category AS category,")), since).mappings()}
        project_rows = [(str(p.id), p.name) for p in projects]
        lever_rows = [(str(l.project_id), l.category, l.enabled, l.updated_at) for l in levers]

    with _seen_lock:
        seen = {project_id: dict(processes) for project_id, processes in _seen.items()}
    empty = {window: {"cost": 0.0, "calls": 0, "blocked": 0, "errors": 0, "tokens": 0, "unpriced": 0}
             for window in ("24h", "7d", "30d")}
    fresh = time.time() - SEEN_WITHIN_SECONDS

    out = []
    for project_id, name in project_rows:
        own = [row for row in lever_rows if row[0] == project_id]
        whole = next((row for row in own if row[1] == ""), None)
        names = {row[1] for row in own if row[1]} | {c for p, c in by_category if p == project_id}
        enabled = {row[1]: row[2] for row in own}
        categories = []
        for category in names:
            row = by_category.get((project_id, category))
            categories.append({"name": category, "enabled": enabled.get(category, True),
                               "windows": _windows(row) if row else empty})
        row = by_project.get(project_id)
        out.append({
            "id": project_id, "name": name, "enabled": whole[2] if whole else True,
            "off_since": whole[3].isoformat() if whole and not whole[2] and whole[3] else None,
            "windows": _windows(row) if row else empty, "categories": categories,
            "reporting": sorted(p for p, at in seen.get(project_id, {}).items() if at >= fresh),
        })
    return {"projects": out}
