#!/usr/bin/python3
# ----------------------------------------------------------------------------
# Module     : procmon/auth.py
# Last updated: 2026-08-21 02:30 EDT
# Description: Minimal, single-owner session auth. This tool can execute
#              arbitrary shell commands on this machine on request --
#              treat reaching it as equivalent to having a terminal open
#              here, and secure it accordingly. A password check alone is
#              NOT sufficient if this is reachable from the open internet
#              -- see the deployment note in app.py's own module
#              docstring for why a tunnel/VPN in front of this matters
#              at least as much as the password itself.
# ----------------------------------------------------------------------------

from __future__ import annotations

import functools
import hmac
import os

from flask import redirect, session, url_for

PROCMON_PASSWORD = os.environ.get("PROCMON_PASSWORD")


def check_password(candidate: str) -> bool:
    if not PROCMON_PASSWORD:
        return False
    # Timing-safe comparison -- a naive == comparison leaks how many
    # leading characters matched via response-time differences, a real
    # (if narrow) attack surface for a tool that can run shell commands.
    return hmac.compare_digest(candidate, PROCMON_PASSWORD)


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped
