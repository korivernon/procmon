#!/usr/bin/python3
# ----------------------------------------------------------------------------
# Module     : procmon/ai_recap.py
# Description: The daily AI usage email, sent at 5 PM Eastern for the 24 hours before it. Plain arithmetic over what
#              procmon already records -- no AI writes it (asked 11/07: "daily ai usage recap ... email", "that
#              doesn't use ai", "sent everyday at 5pm").
#
#              Two kinds of spend, kept apart because they're billed differently:
#                - Apps: every call the ai_shim saw (procmon.ai_calls), billed to the API keys.
#                - Claude Code: the sessions' own transcripts under ~/.claude/projects, billed to the subscription;
#                  only what runs past the plan is charged, as extra usage. Shown at API list rates so the two can be
#                  compared, never as an amount owed.
#
#              Preview without sending:  python3 -m procmon.ai_recap            (from ~/Development)
#              Send now:                 python3 -m procmon.ai_recap --send
# ----------------------------------------------------------------------------

from __future__ import annotations

import argparse
import glob
import html as _html
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text

from procmon import ai_usage, emailer
from procmon.db import get_session

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
SEND_HOUR = 17
CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
RUNAWAY_CALLS_PER_HOUR = 40         # one app making this many calls in an hour gets called out
TOP_SESSIONS = 6


def window_ending(end: datetime | None = None):
    """(start, end) as aware ET datetimes: the 24 hours ending at the last 5 PM (or at `end`)."""
    if end is None:
        now = datetime.now(ET)
        end = now.replace(hour=SEND_HOUR, minute=0, second=0, microsecond=0)
        if end > now:
            end -= timedelta(days=1)
    return end - timedelta(hours=24), end


def _utc_naive(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


# ---- apps (procmon.ai_calls) -----------------------------------------------------------------------------------------

def _app_rows(db, start, end):
    rows = db.execute(text("""
        SELECT p.name AS project, c.category, COALESCE(c.model, '') AS model, c.status,
               count(*) AS calls, sum(c.input_tokens) AS inp, sum(c.output_tokens) AS outp,
               sum(c.cache_write_tokens) AS cw, sum(c.cache_read_tokens) AS cr, sum(c.web_searches) AS ws,
               sum(c.cost) AS cost, count(c.cost) AS priced
        FROM procmon.ai_calls c JOIN procmon.projects p ON p.id = c.project_id
        WHERE c.created_at >= :s AND c.created_at < :e
        GROUP BY 1, 2, 3, 4
    """), {"s": _utc_naive(start), "e": _utc_naive(end)}).mappings().all()
    ints = ("calls", "inp", "outp", "cw", "cr", "ws", "priced")
    return [{**r, **{k: int(r[k] or 0) for k in ints}, "cost": float(r["cost"] or 0)} for r in rows]


def _row_cost(r):
    """Stored cost, or (for rows saved before their model had a price) the cost at today's rates. None = unpriced."""
    if r["priced"] == r["calls"]:
        return float(r["cost"] or 0)
    redo = ai_usage.cost_of(r["model"], *(int(r[k] or 0) for k in ("inp", "outp", "cw", "cr", "ws")))
    return None if redo is None else redo


def apps(start, end):
    with get_session() as db:
        rows = _app_rows(db, start, end)
        hours = db.execute(text("""
            SELECT p.name AS project, date_trunc('hour', c.created_at) AS h, count(*) AS n, max(c.model) AS model
            FROM procmon.ai_calls c JOIN procmon.projects p ON p.id = c.project_id
            WHERE c.created_at >= :s AND c.created_at < :e AND c.status <> 'blocked'
            GROUP BY 1, 2 HAVING count(*) >= :n ORDER BY 1, 2
        """), {"s": _utc_naive(start), "e": _utc_naive(end), "n": RUNAWAY_CALLS_PER_HOUR}).mappings().all()
        prior = []                          # earlier 24h windows that had any calls at all (tracking began 11/05)
        for d in range(1, 8):
            ps, pe = start - timedelta(days=d), end - timedelta(days=d)
            rows_d = _app_rows(db, ps, pe)
            if rows_d:
                prior.append(sum(c for c in (_row_cost(r) for r in rows_d) if c))

    projects = defaultdict(lambda: {"calls": 0, "cost": 0.0, "unpriced": 0, "blocked": 0, "errors": 0, "no_tokens": 0,
                                    "models": defaultdict(int), "tokens_in": 0, "tokens_out": 0})
    categories = defaultdict(lambda: {"calls": 0, "cost": 0.0, "models": defaultdict(int), "tokens_in": 0,
                                      "tokens_out": 0, "unpriced": 0, "errors": 0})
    for r in rows:
        p = projects[r["project"]]
        if r["status"] == "blocked":
            p["blocked"] += r["calls"]
            continue
        if r["status"] == "error":
            p["errors"] += r["calls"]
        cost = _row_cost(r)
        p["calls"] += r["calls"]
        if r["status"] == "ok" and not (r["inp"] or r["outp"] or r["cr"] or r["cw"]):
            p["no_tokens"] += r["calls"]
        p["models"][r["model"] or "unknown"] += r["calls"]
        p["tokens_in"] += (r["inp"] or 0) + (r["cr"] or 0) + (r["cw"] or 0)
        p["tokens_out"] += r["outp"] or 0
        c = categories[(r["project"], r["category"])]
        c["calls"] += r["calls"]
        c["models"][r["model"] or "unknown"] += r["calls"]
        c["tokens_in"] += (r["inp"] or 0) + (r["cr"] or 0) + (r["cw"] or 0)
        c["tokens_out"] += r["outp"] or 0
        if r["status"] == "error":
            c["errors"] += r["calls"]
        if cost is None:
            p["unpriced"] += r["calls"]
            c["unpriced"] += r["calls"]
        else:
            p["cost"] += cost
            c["cost"] += cost

    runaways = []
    for h in hours:
        when = h["h"].replace(tzinfo=timezone.utc).astimezone(ET)
        runaways.append({"project": h["project"], "hour": when, "calls": h["n"], "model": h["model"]})

    total = sum(p["cost"] for p in projects.values())
    avg7 = sum(prior) / len(prior) if prior else None
    return {"projects": dict(projects), "categories": categories, "total": total, "avg7": avg7,
            "avg_days": len(prior), "runaways": _runs(runaways)}


def _runs(hours):
    """Back-to-back busy hours of one app become one run: start, end, calls."""
    out = []
    for h in hours:
        last = out[-1] if out else None
        if last and last["project"] == h["project"] and h["hour"] - last["end"] <= timedelta(hours=1):
            last["end"], last["calls"] = h["hour"], last["calls"] + h["calls"]
            last["hours"] += 1
            last["models"].add(h["model"])
        else:
            out.append({"project": h["project"], "start": h["hour"], "end": h["hour"], "calls": h["calls"],
                        "hours": 1, "models": {h["model"]}})
    return out


# ---- Claude Code (session transcripts) -------------------------------------------------------------------------------

def claude_code(start, end):
    """Per model and per session: responses, tokens and the cost at API list rates, from every transcript touched in
    the window (subagent transcripts count toward the session that started them)."""
    s_utc, e_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    cutoff = s_utc.timestamp()
    seen = {}                               # message id -> (session, model, usage); a reply is logged more than once
    titles = {}
    files = glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl")) + \
        glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*", "subagents", "*.jsonl"))
    for path in files:
        try:
            if os.path.getmtime(path) < cutoff:
                continue
            f = open(path, errors="replace")
        except OSError:
            continue
        project_dir = os.path.basename(os.path.dirname(path) if "subagents" not in path
                                       else os.path.dirname(os.path.dirname(os.path.dirname(path))))
        with f:
            for line in f:
                if '"custom-title"' in line or '"ai-title"' in line:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    sid = d.get("sessionId")
                    t = d.get("customTitle") or d.get("aiTitle")
                    if sid and t and (d.get("type") == "custom-title" or sid not in titles):
                        titles[sid] = t
                    continue
                if '"assistant"' not in line or '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "assistant":
                    continue
                ts = d.get("timestamp")
                m = d.get("message") or {}
                u, model, mid = m.get("usage"), m.get("model"), m.get("id")
                if not (ts and u and model and mid) or model.startswith("<"):
                    continue
                try:
                    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if not s_utc <= t < e_utc:
                    continue
                seen[mid] = (d.get("sessionId") or "?", project_dir, model, u)

    models = defaultdict(lambda: {"responses": 0, "inp": 0, "out": 0, "cw": 0, "cr": 0, "cost": 0.0, "unpriced": 0})
    sessions = defaultdict(lambda: {"cost": 0.0, "responses": 0, "models": defaultdict(float), "dir": ""})
    for sid, pdir, model, u in seen.values():
        inp, out = u.get("input_tokens") or 0, u.get("output_tokens") or 0
        cw, cr = u.get("cache_creation_input_tokens") or 0, u.get("cache_read_input_tokens") or 0
        ws = ((u.get("server_tool_use") or {}).get("web_search_requests")) or 0
        cost = ai_usage.cost_of(model, inp, out, cw, cr, ws)
        mm = models[model]
        mm["responses"] += 1
        mm["inp"] += inp
        mm["out"] += out
        mm["cw"] += cw
        mm["cr"] += cr
        s = sessions[sid]
        s["responses"] += 1
        s["dir"] = pdir
        if cost is None:
            mm["unpriced"] += 1
        else:
            mm["cost"] += cost
            s["cost"] += cost
            s["models"][model] += cost
    for sid, s in sessions.items():
        s["title"] = titles.get(sid) or _short_dir(s["dir"])
    return {"models": dict(models), "sessions": sessions, "total": sum(m["cost"] for m in models.values())}


def _short_dir(d):
    d = d.replace("-Users-trapbookpro-Development-", "").replace("-Users-trapbookpro", "home")
    return d.split("--claude-worktrees-")[0] or d


# ---- the email --------------------------------------------------------------------------------------------------------

def _usd(x):
    if x == 0:
        return "$0"
    return f"${x:,.2f}" if x >= 0.995 else f"${x:.3f}" if x >= 0.001 else "<$0.001"


def _n(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def _avg_label(a):
    if a["avg7"] is None:
        return "no earlier days to compare"
    days = a["avg_days"]
    return f"usual day {_usd(a['avg7'])}" + ("" if days >= 7 else f" ({days} day{'s' if days != 1 else ''} of history)")


def _tok(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def _model(m):
    return (m or "").replace("claude-", "").replace("-20251001", "")


def _hour(dt):
    return dt.strftime("%-I %p").lower().replace(" ", "")


def build(start, end):
    a, c = apps(start, end), claude_code(start, end)
    span = f"{start.strftime('%a %b %-d')} 5pm – {end.strftime('%a %b %-d')} 5pm ET"
    subject = f"AI usage {end.strftime('%a %b %-d')} · apps {_usd(a['total'])} · Claude Code ~{_usd(c['total'])}"

    notes = []
    for r in a["runaways"]:
        models = ", ".join(sorted(_model(m) for m in r["models"] if m))
        if r["hours"] == 1:
            notes.append(f"{r['project']} made {r['calls']} calls in the {_hour(r['start'])} hour ({models}).")
        else:
            notes.append(f"{r['project']} ran hot for {r['hours']} hours, {_hour(r['start'])} to "
                         f"{_hour(r['end'] + timedelta(hours=1))}: {r['calls']} calls, about "
                         f"{round(r['calls'] / r['hours'])} an hour ({models}).")
    for name, p in a["projects"].items():
        if p["unpriced"]:
            notes.append(f"{name}: {_n(p['unpriced'], 'call')} on a model with no price, not in the total.")
        if p["blocked"]:
            notes.append(f"{name}: {_n(p['blocked'], 'call')} stopped by a lever.")
        if p["errors"]:
            notes.append(f"{name}: {_n(p['errors'], 'call')} failed.")
        if p["no_tokens"]:
            notes.append(f"{name}: {_n(p['no_tokens'], 'call')} came back with no token counts, so their cost "
                         "isn't known.")
    if a["avg7"] and a["avg7"] >= 0.5 and a["total"] >= 2 * a["avg7"]:
        notes.append(f"Apps spent {a['total'] / a['avg7']:.1f}x their usual day ({_usd(a['avg7'])}).")
    for m, mm in c["models"].items():
        if mm["unpriced"]:
            notes.append(f"Claude Code: {mm['unpriced']} replies on {_model(m)}, which has no price yet.")

    projects = sorted(a["projects"].items(), key=lambda kv: (-kv[1]["cost"], -kv[1]["calls"]))
    uses = defaultdict(list)                # project -> its use cases, costliest first
    for (proj, cat), v in sorted(a["categories"].items(), key=lambda kv: (-kv[1]["cost"], -kv[1]["calls"])):
        uses[proj].append((cat, v))
    cmodels = sorted(c["models"].items(), key=lambda kv: -kv[1]["cost"])
    sessions = sorted(c["sessions"].values(), key=lambda s: -s["cost"])[:TOP_SESSIONS]

    def use_line(v):
        extra = [f"{v['unpriced']} unpriced"] if v["unpriced"] else []
        extra += [f"{v['errors']} failed"] if v["errors"] else []
        avg = _usd(v["cost"] / v["calls"]) + "/call" if v["calls"] and v["cost"] else ""
        return ", ".join(x for x in [avg, f"{_tok(v['tokens_in'])} in / {_tok(v['tokens_out'])} out"] + extra if x)

    def models_of(p):
        return ", ".join(f"{_model(m)} ×{n}" for m, n in sorted(p["models"].items(), key=lambda kv: -kv[1]))

    # plain text
    t = [f"AI usage, {span}", "",
         f"Apps (API keys): {_usd(a['total'])}   {_avg_label(a)}",
         f"Claude Code (subscription, at API rates): ~{_usd(c['total'])}", ""]
    if notes:
        t += ["Worth a look:"] + [f"  - {n}" for n in notes] + [""]
    t.append("Apps")
    for name, p in projects:
        t.append(f"  {name:<16} {_usd(p['cost']):>9}  {_n(p['calls'], 'call'):>9}  {models_of(p)}")
    if not projects:
        t.append("  no AI calls")
    for name, p in projects:
        if not uses[name]:
            continue
        t += ["", f"{name} by use"]
        for cat, v in uses[name]:
            t.append(f"  {cat:<24} {_usd(v['cost']):>9}  {_n(v['calls'], 'call'):>9}  {models_of(v)}  ({use_line(v)})")
    t += ["", "Claude Code by model"]
    for m, mm in cmodels:
        t.append(f"  {_model(m):<14} ~{_usd(mm['cost']):>9}  {mm['responses']:>5} replies  "
                 f"{_tok(mm['cr'])} cache read, {_tok(mm['cw'])} cache write, {_tok(mm['out'])} out")
    if sessions:
        t += ["", "Biggest Claude Code sessions"]
        for s in sessions:
            top = max(s["models"], key=s["models"].get) if s["models"] else ""
            t.append(f"  ~{_usd(s['cost']):>9}  {s['title'][:60]}  ({_model(top)}, {s['responses']} replies)")
    t += ["", "Claude Code is covered by the subscription; only use past the plan is billed, as extra usage.",
          f"Levers and live numbers: {os.environ.get('PROCMON_BASE_URL', 'http://127.0.0.1:8600').rstrip('/')}/ai"]
    body = "\n".join(t)

    # html
    e = _html.escape
    th = 'style="text-align:left;padding:4px 10px 4px 0;color:#666;font-weight:600;border-bottom:1px solid #ddd"'
    td = 'style="padding:4px 10px 4px 0;border-bottom:1px solid #f0f0f0;vertical-align:top"'
    tdr = 'style="padding:4px 10px 4px 0;border-bottom:1px solid #f0f0f0;text-align:right;white-space:nowrap"'

    def table(head, rows):
        h = "".join(f"<th {th}>{e(x)}</th>" for x in head)
        b = "".join("<tr>" + "".join(f"<td {tdr if i in (1, 2) else td}>{e(str(x))}</td>" for i, x in enumerate(r))
                    + "</tr>" for r in rows)
        return f'<table style="border-collapse:collapse;font-size:14px;margin:6px 0 18px">{"<tr>" + h + "</tr>"}{b}</table>'

    parts = [f'<div style="font-family:-apple-system,Segoe UI,sans-serif;color:#111;max-width:680px">',
             f'<p style="color:#666;margin:0 0 10px">{e(span)}</p>',
             '<table style="border-collapse:collapse;margin-bottom:16px"><tr>'
             f'<td style="padding:8px 28px 8px 0"><div style="font-size:26px;font-weight:700">{_usd(a["total"])}</div>'
             f'<div style="color:#666;font-size:13px">apps · {e(_avg_label(a))}</div></td>'
             f'<td style="padding:8px 0"><div style="font-size:26px;font-weight:700">~{_usd(c["total"])}</div>'
             '<div style="color:#666;font-size:13px">Claude Code at API rates</div></td></tr></table>']
    if notes:
        parts.append('<div style="background:#fff6e5;border:1px solid #f0c36d;border-radius:8px;padding:8px 12px;'
                     'margin-bottom:16px"><b>Worth a look</b><ul style="margin:4px 0 0 18px;padding:0">'
                     + "".join(f"<li>{e(n)}</li>" for n in notes) + "</ul></div>")
    parts.append("<h3 style='margin:8px 0 0'>Apps</h3>")
    parts.append(table(["App", "Cost", "Calls", "Models"],
                       [[n, _usd(p["cost"]), p["calls"], models_of(p)] for n, p in projects]) if projects
                 else "<p>No AI calls.</p>")
    for name, p in projects:
        if not uses[name]:
            continue
        parts.append(f"<h3 style='margin:8px 0 0'>{e(name)} by use</h3>")
        parts.append(table(["Use", "Cost", "Calls", "Models · detail"],
                           [[cat, _usd(v["cost"]), v["calls"], f"{models_of(v)} · {use_line(v)}"]
                            for cat, v in uses[name]]))
    parts.append("<h3 style='margin:8px 0 0'>Claude Code by model</h3>")
    parts.append(table(["Model", "At API rates", "Replies", "Tokens"],
                       [[_model(m), "~" + _usd(mm["cost"]), mm["responses"],
                         f"{_tok(mm['cr'])} cache read · {_tok(mm['cw'])} cache write · {_tok(mm['out'])} out"]
                        for m, mm in cmodels]))
    if sessions:
        parts.append("<h3 style='margin:8px 0 0'>Biggest Claude Code sessions</h3>")
        parts.append(table(["Session", "At API rates", "Replies", "Mostly"],
                           [[s["title"][:70], "~" + _usd(s["cost"]), s["responses"],
                             _model(max(s["models"], key=s["models"].get)) if s["models"] else ""] for s in sessions]))
    parts.append('<p style="color:#666;font-size:13px">Claude Code is covered by the subscription; only use past the '
                 'plan is billed, as extra usage. '
                 f'<a href="{e(os.environ.get("PROCMON_BASE_URL", "http://127.0.0.1:8600").rstrip("/"))}/ai">'
                 'Levers and live numbers</a></p></div>')
    return subject, body, "".join(parts)


def send_daily():
    """The 5 PM job. Never raises: a failed recap is logged, not a scheduler crash."""
    if not emailer.PROCMON_NOTIFY_EMAIL:
        logger.info("AI recap skipped: PROCMON_NOTIFY_EMAIL is not set")
        return
    try:
        subject, body, html = build(*window_ending())
        emailer.send_email(emailer.PROCMON_NOTIFY_EMAIL, subject, body, html=html)
        logger.info("AI recap sent: %s", subject)
    except Exception:
        logger.exception("AI recap failed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="procmon's daily AI usage recap")
    ap.add_argument("--send", action="store_true", help="email it now instead of printing it")
    a = ap.parse_args()
    try:                # run by hand: the same settings the procmon service reads
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
        emailer.PROCMON_NOTIFY_EMAIL = os.environ.get("PROCMON_NOTIFY_EMAIL", "")
    except ImportError:
        pass
    if a.send:
        logging.basicConfig(level=logging.INFO)
        send_daily()
    else:
        subj, text_body, _ = build(*window_ending())
        print(subj, "\n", text_body, sep="")
