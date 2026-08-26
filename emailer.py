#!/usr/bin/python3
# ----------------------------------------------------------------------------
# Module     : procmon/emailer.py
# Description: Crash-notification email, as a second, independent channel
#              alongside photon_client's iMessage text. Exists because the
#              text channel has a confirmed single point of failure: the
#              photon-notif relay is ITSELF one of the processes procmon
#              monitors, so when a machine-wide event takes several
#              processes down at once, the relay tends to be among the
#              casualties and every crash text for that cycle fails
#              (observed 2026-08-25: relay down/EADDRINUSE-looping during
#              a crash storm meant zero texts went out). SMTP goes
#              straight out to smtp.gmail.com and shares no local
#              infrastructure with the relay.
#
#              Deliberately does NOT store the SMTP password anywhere in
#              procmon. pomtrader already keeps the info@ahiasolutions.com
#              Gmail app password encrypted at rest (Fernet) in
#              pomtrader.notification_settings, in the very same Postgres
#              instance procmon's own schema lives in -- so this module
#              reads that row and decrypts at send time, exactly like
#              pomtrader's dashboard/notifications.py does. The Fernet key
#              (SETTINGS_ENCRYPTION_KEY) is read from procmon's own env if
#              set, else parsed out of pomtrader's .env on disk -- a path
#              reference, not a copied secret.
#
#              PROCMON_NOTIFY_EMAIL (in procmon's .env) is the recipient;
#              unset means email notifications are simply off, mirroring
#              how PROCMON_NOTIFY_PHONE gates texting.
# ----------------------------------------------------------------------------

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.text import MIMEText

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text

logger = logging.getLogger(__name__)

PROCMON_NOTIFY_EMAIL = os.environ.get("PROCMON_NOTIFY_EMAIL", "")
POMTRADER_ENV_PATH = os.environ.get(
    "PROCMON_POMTRADER_ENV",
    os.path.expanduser("~/Development/pomtrader/.env"),
)


class EmailError(Exception):
    pass


def _encryption_key() -> str:
    key = os.environ.get("SETTINGS_ENCRYPTION_KEY")
    if key:
        return key
    try:
        with open(POMTRADER_ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line.startswith("SETTINGS_ENCRYPTION_KEY="):
                    return line.split("=", 1)[1].strip()
    except OSError as e:
        raise EmailError(f"could not read {POMTRADER_ENV_PATH} for SETTINGS_ENCRYPTION_KEY: {e}")
    raise EmailError(f"SETTINGS_ENCRYPTION_KEY not set and not found in {POMTRADER_ENV_PATH}")


def _smtp_settings() -> dict:
    # Late import so a procmon that never emails never touches the DB for this.
    from procmon.db import get_session

    with get_session() as db:
        row = db.execute(text(
            # profile_id IS NULL is the original pre-profiles row -- same
            # credentials, still the canonical default config
            "SELECT smtp_host, smtp_port, smtp_username, smtp_from_email, smtp_password_encrypted "
            "FROM pomtrader.notification_settings WHERE profile_id = 1 OR profile_id IS NULL "
            "ORDER BY profile_id NULLS LAST LIMIT 1"
        )).fetchone()
    if row is None or not all([row.smtp_host, row.smtp_username, row.smtp_from_email, row.smtp_password_encrypted]):
        raise EmailError("pomtrader.notification_settings has no usable SMTP config (profile_id 1 or NULL)")
    try:
        password = Fernet(_encryption_key().encode()).decrypt(row.smtp_password_encrypted.encode()).decode()
    except InvalidToken:
        raise EmailError("could not decrypt stored SMTP password -- SETTINGS_ENCRYPTION_KEY may have changed since it was saved")
    return {
        "host": row.smtp_host,
        "port": row.smtp_port or 587,
        "username": row.smtp_username,
        "from_email": row.smtp_from_email,
        "password": password,
    }


def send_email(to_address: str, subject: str, body: str) -> None:
    settings = _smtp_settings()
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = settings["from_email"]
    msg["To"] = to_address

    try:
        with smtplib.SMTP(settings["host"], settings["port"], timeout=15) as server:
            server.starttls()
            server.login(settings["username"], settings["password"])
            server.sendmail(settings["from_email"], [to_address], msg.as_string())
    except (smtplib.SMTPException, OSError) as e:
        raise EmailError(f"SMTP send failed: {e}")
