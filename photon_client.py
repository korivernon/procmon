#!/usr/bin/python3
# ----------------------------------------------------------------------------
# Module     : procmon/photon_client.py
# Description: Talks to the local `photon-notif` Node/Spectrum service (see
#              photon-notif/src/index.ts -- procmon/photon-notif is a symlink
#              to pomtrader's own photon-notif; it's ONE shared relay/phone
#              line, not a separate instance) over a plain localhost HTTP
#              call to send an outbound crash-notification text. procmon
#              itself never touches the Spectrum SDK directly -- Node owns
#              the gRPC-backed provider connection, Python just asks it to
#              send text. PHOTON_NOTIF_URL and PHOTON_SHARED_SECRET must
#              match the same env vars photon-notif/.env defines.
#
#              Deliberately outbound-only from procmon's side -- unlike
#              pomtrader's own dashboard/photon_client.py, there is no
#              inbound webhook wired up here. photon-notif's inbound "Yes"
#              handling is pomtrader's alone (ai_gerry trade approval);
#              adding a second, competing reply-driven flow on the same
#              shared line would make a bare "Yes" ambiguous between the
#              two. See _notify_crash() in app.py for the reasoning.
# ----------------------------------------------------------------------------

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

PHOTON_NOTIF_URL = os.environ.get("PHOTON_NOTIF_URL", "http://127.0.0.1:8790")
PHOTON_SHARED_SECRET = os.environ.get("PHOTON_SHARED_SECRET", "")


class PhotonError(Exception):
    pass


def send_imessage(phone: str, text: str) -> None:
    if not PHOTON_SHARED_SECRET:
        raise PhotonError("PHOTON_SHARED_SECRET not set -- see photon-notif/.env and procmon's own .env")
    try:
        resp = requests.post(
            f"{PHOTON_NOTIF_URL}/send",
            json={"phone": phone, "text": text},
            headers={"X-Photon-Secret": PHOTON_SHARED_SECRET},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise PhotonError(f"photon-notif send failed (is it running? npm run start in photon-notif/): {e}")
