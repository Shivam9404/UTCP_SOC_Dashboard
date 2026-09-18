"""
server.py

Wraps fetch_incidents.py as a small live API so the dashboard runs
completely on its own -- nobody needs to run any script by hand, ever, once
this process is deployed and left running (Render, systemd, pm2, Docker,
whatever fits your setup).

What it does:
- Runs the same logic as fetch_incidents.py on a background schedule
  (refresh every REFRESH_INTERVAL_MINUTES, default 1; end-of-day email
  check at 23:59) using APScheduler -- nobody needs to run any script by
  hand, this process does it on its own the whole time it's running.
- Exposes GET /api/incidents/latest, returning today's flattened incidents
  (used for the "Total incidents today" style KPI cards).
- Exposes GET /api/incidents/open, returning every currently-open incident
  regardless of creation date (any non-closed status: In Progress, Pending
  Client, Pending SOC, On Hold, Blocked, etc.) -- use this for the Open
  Incidents table so older still-open tickets don't disappear just
  because they weren't created today.
- Exposes GET /api/incidents/pending, returning the Pending with Client /
  Pending with SOC / On Hold totals and per-assignee breakdown, computed
  from the pending window (see PENDING_SINCE_DATE in fetch_incidents.py).
- Exposes POST /api/refresh to force an immediate poll on demand.
- Exposes GET /healthz, which also reports the last refresh error (if any)
  and how long ago the last successful refresh was -- check this first if
  the dashboard looks stuck.
- At end of day, sends the SOC team a summary of everything still pending
  (today's plus older), plus a personal reminder email to each individual
  assignee for their own still-open items.

REFRESH_INTERVAL_MINUTES is now 1 by default, so the dashboard reflects a
new/updated detection within about a minute of it landing in Vigilhawk,
with no manual runs ever needed -- this process, left running, is the whole
"live data" pipeline. If Vigilhawk enforces an API rate limit, check it
before leaving this at 1; raise the number (e.g. "2" or "5") in .env via
REFRESH_INTERVAL_MINUTES if you start seeing 429s in the logs or in
/healthz's lastError.

Every refresh is wrapped so a single failed API call (bad credentials, a
network blip, a rate limit) is logged and recorded in /healthz, but never
kills the process -- the scheduler just tries again next interval.

Local test: python server.py
Render start command: gunicorn -w 1 -b 0.0.0.0:$PORT server:app
(-w 1 matters -- keep exactly one worker so there's a single in-memory cache
and a single scheduler. If you need more workers later, move the cache to
Redis or similar.)
"""

import os
import logging
import atexit
from datetime import datetime

from flask import Flask, jsonify
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor

from fetch_incidents import (
    fetch_today_incidents,
    fetch_pending_window_incidents,
    flatten_incident,
    save_json,
    build_pending_summary,
    save_pending_json,
    send_open_incident_email,
    send_assignee_reminder_emails,
    TIMEZONE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("soc-dashboard")

app = Flask(__name__)
CORS(app)  # allow the Vercel-hosted frontend to call this API cross-origin

# How often this server polls the Vigilhawk API on its own, with nobody
# needing to run anything by hand. Default is 1 minute -- lower it further
# only if your API can comfortably take that; check Vigilhawk's rate limit
# first. If PENDING_SINCE_DATE (in fetch_incidents.py) pulls a lot of
# history, a single refresh can take a while -- if refreshes are taking
# longer than this interval (see the "Refreshed:" timing in the logs),
# raise REFRESH_INTERVAL_MINUTES or set PENDING_SINCE_DATE to a more recent
# date so each fetch is smaller.
REFRESH_INTERVAL_MINUTES = int(os.getenv("REFRESH_INTERVAL_MINUTES", "1"))

_cache = {"date": None, "generatedAt": None, "count": 0, "incidents": []}
_pending_cache = {"generatedAt": None, "sinceDate": None, "lookbackDays": None, "totals": {}, "byAssignee": []}
# Every currently-open incident (any non-closed status -- In Progress,
# Pending Client, Pending SOC, On Hold, Blocked, etc.), regardless of what
# day it was created. Unlike _cache (today's fetch only), this is built
# from the wide pending window, so an incident that's been "In Progress"
# since last week still shows up in the Open Incidents table today.
_open_cache = {"generatedAt": None, "count": 0, "incidents": []}

# Tracks refresh health so /healthz (and you) can tell at a glance whether
# the background job is actually succeeding, instead of guessing from a
# dashboard that just looks stale.
_status = {
    "lastSuccessAt": None,
    "lastAttemptAt": None,
    "lastError": None,
    "refreshInProgress": False,
}


def refresh(eod=False):
    """Does one full refresh cycle. Never raises -- any failure is caught,
    logged, and recorded in _status so it's visible via /healthz, but the
    server and scheduler keep running either way."""
    _status["lastAttemptAt"] = datetime.now(TIMEZONE).isoformat()
    _status["refreshInProgress"] = True
    try:
        today, raw = fetch_today_incidents()
        flattened = [flatten_incident(i) for i in raw]
        save_json(today, flattened)  # still writes local JSON too -- handy for debugging
        _cache.update({
            "date": today.isoformat(),
            "generatedAt": datetime.now(TIMEZONE).isoformat(),
            "count": len(flattened),
            "incidents": flattened,
        })

        pending_window_flattened = fetch_pending_window_incidents()
        pending_summary = build_pending_summary(pending_window_flattened)
        save_pending_json(pending_summary)
        _pending_cache.update(pending_summary)

        open_incidents_all = [i for i in pending_window_flattened if not i["isClosed"]]
        open_incidents_all.sort(key=lambda i: i.get("severityId") if i.get("severityId") is not None else 9)
        _open_cache.update({
            "generatedAt": _cache["generatedAt"],
            "count": len(open_incidents_all),
            "incidents": open_incidents_all,
        })

        log.info(
            "Refreshed: %d incidents today for %s; %d open overall; pending totals: %s",
            len(flattened), today, len(open_incidents_all), pending_summary["totals"],
        )

        _status["lastError"] = None
        _status["lastSuccessAt"] = _cache["generatedAt"]

        if eod:
            send_open_incident_email(today, open_incidents_all)
            send_assignee_reminder_emails(today, open_incidents_all)

    except Exception as exc:  # noqa: BLE001 -- deliberately broad: this must never kill the process
        log.exception("Refresh failed: %s", exc)
        _status["lastError"] = str(exc)
    finally:
        _status["refreshInProgress"] = False


@app.route("/api/incidents/latest")
def latest():
    return jsonify(_cache)


@app.route("/api/incidents/pending")
def pending():
    return jsonify(_pending_cache)


@app.route("/api/incidents/open")
def open_incidents():
    """Every currently-open incident regardless of creation date -- use
    this for the Open Incidents table instead of /api/incidents/latest,
    so older In Progress / Pending Client / Pending SOC / On Hold /
    Blocked tickets don't disappear just because they weren't created
    today."""
    return jsonify(_open_cache)


@app.route("/healthz")
def healthz():
    """Check this first if the dashboard looks stuck. lastError will show
    exactly why a refresh failed; lastSuccessAt tells you how stale the
    cache currently is."""
    return jsonify({"status": "ok", **_status})


@app.route("/api/refresh", methods=["POST"])
def force_refresh():
    """Manual escape hatch -- e.g. to pull in a detection you know just
    landed, without waiting for the next scheduled poll."""
    refresh()
    if _status["lastError"]:
        return jsonify({"status": "error", "error": _status["lastError"]}), 502
    return jsonify({"status": "refreshed", "generatedAt": _cache["generatedAt"]})


# max_instances=1 + coalesce=True (the defaults) mean: if one refresh is
# still running when the next interval hits, that firing is skipped rather
# than piling up -- but skipped firings were happening *silently* before.
# Now a slow or failing refresh shows up in the logs and in /healthz
# instead of just looking like the dashboard stopped updating.
scheduler = BackgroundScheduler(
    timezone=str(TIMEZONE),
    executors={"default": ThreadPoolExecutor(1)},
    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
)
scheduler.add_job(refresh, "interval", minutes=REFRESH_INTERVAL_MINUTES, id="refresh")
scheduler.add_job(lambda: refresh(eod=True), "cron", hour=23, minute=59, id="eod")
scheduler.start()
atexit.register(lambda: scheduler.shutdown(wait=False))

refresh()  # populate the cache immediately on startup -- wrapped, so a
           # failure here logs and leaves the server startable instead of
           # crashing before app.run() is even reached.

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), threaded=True)