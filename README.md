# procmon

A standalone process monitor -- start/stop/restart scripts and servers,
view their logs, and see status at a glance. Separate from any other
project; shares only the underlying Postgres instance, in its own
"procmon" schema. Installable as a home-screen app (PWA).

## Setup

    pip install -r requirements.txt

Set required env vars (put these in a .env file and load it, or export
them directly -- this app does not currently call load_dotenv() itself):

    PROCMON_PASSWORD=<a real password>
    FLASK_SECRET_KEY=<generate with: python3 -c "import secrets; print(secrets.token_hex(32))">
    PROCMON_DATABASE_URL=postgresql+psycopg2://admin@127.0.0.1:5433/postgres   # optional, this is the default
    PROCMON_LOG_DIR=/path/to/log/directory                                     # optional, defaults to /tmp/procmon_logs
    PROCMON_PORT=8600                                                          # optional

## Run

    python3 -m procmon.app

## Two ways to run a process

**Perpetual** -- starts it and keeps it running. If it crashes
unexpectedly, procmon auto-restarts it. If it crashes 3+ times within 5
minutes, auto-restart backs off and stops trying (rather than
relaunching a genuinely broken command every 15 seconds forever) --
status stays red until you fix it and start it manually again.

**Scheduled** -- runs on a cron schedule (standard 5-field cron syntax,
e.g. `0 9 * * *` for 9 AM daily), then exits until its next scheduled
time. There's no manual start/stop for these -- status reflects whether
the *last* scheduled run succeeded, not whether it's currently running
(sitting idle between runs is the normal, healthy state for these).

## IMPORTANT -- before exposing this anywhere reachable "on the go"

This tool executes arbitrary shell commands on request. Reaching it is
equivalent to having a terminal open on this machine. The password
check is real, but is NOT sufficient on its own if this is exposed
directly to the open internet -- put it behind Tailscale, a Cloudflare
Tunnel, or a VPN, so that "reachable at all" already implies "a device
you trust". This is the actual, correct way to get real "access
anywhere" for a tool with this capability, not an optional extra step.

This same requirement also solves a second, separate problem: full PWA
installability (the Android/Chrome "Add to Home Screen" prompt
specifically, via its service worker) requires HTTPS -- plain HTTP
won't register a service worker at all except on localhost. Tailscale
in particular is a genuinely convenient way to get both real security
AND working HTTPS at once: `tailscale serve https / http://localhost:8600`
(or the equivalent for whichever port you run this on) gives you a
real TLS cert for your own tailnet automatically, with access limited
to devices you've actually authorized.

## Installing to your home screen

Once running behind HTTPS (see above):

**iOS (Safari):** open the app's URL, tap the Share icon, tap "Add to
Home Screen". iOS uses its own tags for this (already included) rather
than relying on the standard web manifest the way Android does.

**Android (Chrome):** open the app's URL -- Chrome should offer an
"Install app" / "Add to Home Screen" prompt automatically once it
detects the manifest and a successfully registered service worker,
both already wired in.

Either way, it opens without browser chrome (address bar, tabs) --
full-screen, feeling like a native app, per the manifest's own
`"display": "standalone"` setting.
