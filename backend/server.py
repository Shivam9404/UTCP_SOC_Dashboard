"""
server.py

Wraps fetch_incidents.py as a small live API so the dashboard can be deployed
on the web: frontend on Vercel, this on Render.

What it does:
- Runs the same logic as fetch_incidents.py on a background schedule
  (refresh every 10 min, end-of-day email check at 23:59) using APScheduler.
- Exposes GET /api/incidents/latest, returning today's flattened incidents
  as JSON -- this is what the deployed frontend calls instead of reading a
  local file.

Local test: python server.py
Render start command: gunicorn -w 1 -b 0.0.0.0:$PORT server:app
(-w 1 matters -- keep exactly one worker so there's a single in-memory cache
and a single scheduler. If you need more workers later, move the cache to
Redis or similar.)
"""

import os
from datetime import datetime

from flask import Flask, jsonify
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler

from fetch_incidents import (
    fetch_today_incidents,
    flatten_incident,
    save_json,
    send_open_incident_email,
    TIMEZONE,
)

app = Flask(__name__)
CORS(app)  # allow the Vercel-hosted frontend to call this API cross-origin

_cache = {"date": None, "generatedAt": None, "count": 0, "incidents": []}


def refresh(eod=False):
    today, raw = fetch_today_incidents()
    flattened = [flatten_incident(i) for i in raw]
    save_json(today, flattened)  # still writes local JSON too -- handy for debugging on Render's shell
    _cache.update({
        "date": today.isoformat(),
        "generatedAt": datetime.now(TIMEZONE).isoformat(),
        "count": len(flattened),
        "incidents": flattened,
    })
    print(f"Refreshed: {len(flattened)} incidents for {today}")
    if eod:
        open_incidents = [i for i in flattened if not i["isClosed"]]
        send_open_incident_email(today, open_incidents)


@app.route("/api/incidents/latest")
def latest():
    return jsonify(_cache)


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


scheduler = BackgroundScheduler(timezone=str(TIMEZONE))
scheduler.add_job(refresh, "interval", minutes=10, id="refresh")
scheduler.add_job(lambda: refresh(eod=True), "cron", hour=23, minute=59, id="eod")
scheduler.start()

refresh()  # populate the cache immediately on startup, don't wait 10 min

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
