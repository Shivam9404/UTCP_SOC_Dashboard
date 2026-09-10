# SOC incident dashboard

Two pieces:

- **backend/** — `fetch_incidents.py` pulls today's incidents from your API,
  flattens them, and writes JSON that the dashboard reads. Also sends the
  end-of-day escalation email.
- **frontend/** — a small React (Vite) app that renders the Style A
  ops-center dashboard: KPI cards, severity donut, peak-hour chart, an open
  incidents table, a closed/open-by-client breakdown, and a top-resolvers
  leaderboard.

## 1. Backend setup

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env
# edit .env: API_BASE_URL is already filled in, add API_KEY / API_SECRET,
# and your SMTP + SOC email details for the end-of-day alert.
```

Then do a test run:

```bash
python fetch_incidents.py
```

This writes `frontend/public/data/incidents_YYYY-MM-DD.json` and
`incidents_latest.json`. Open the newest file and sanity-check the field
names — if your API's actual response shape, auth scheme, or pagination
params differ from what's assumed (see the comments at the top of
`fetch_incidents.py`), send me the real request/response from your API docs
or Postman collection and I'll adjust `auth_headers()` / `fetch_page()`.

**Important — confirm what "closed" means for your tenant.** The script
currently treats `incidentStatus == 2` (or a populated `resolutionDateTime`)
as closed, set via `CLOSED_STATUS_CODE` in `.env`. Check a few known-closed
incidents in your system and confirm that code is right.

## 2. Keep it running through the day (cron)

The dashboard always shows "today" (12:00 AM–11:59 PM in `TIMEZONE`, default
`Asia/Kolkata`). Refresh the data regularly and run the end-of-day check
once, right before midnight:

```cron
# Refresh every 10 minutes during the day
*/10 6-23 * * * cd /path/to/soc_dashboard/backend && /usr/bin/python3 fetch_incidents.py >> fetch.log 2>&1

# End-of-day: final refresh + email anything still open
59 23 * * * cd /path/to/soc_dashboard/backend && /usr/bin/python3 fetch_incidents.py --eod >> fetch.log 2>&1
```

At midnight the next day's run naturally starts a fresh file
(`incidents_2026-09-11.json`, etc.) and `incidents_latest.json` flips over to
the new day — the dashboard picks this up on its next poll (every 60s) with
no restart needed.

If you'd rather not manage cron, an alternative is running
`fetch_incidents.py` in a loop with `schedule` or `APScheduler` as a
long-lived process — say the word and I'll convert it.

## 3. Frontend setup

```bash
cd frontend
npm install
npm run dev
```

Open the printed local URL. The dashboard polls
`/data/incidents_latest.json` every 60 seconds, so once cron is refreshing
that file, the dashboard stays live without any manual reload.

For a production deployment, `npm run build` produces a static `dist/`
folder — serve it with nginx/Caddy/etc., and point `OUTPUT_DIR` in the
backend `.env` at that same build's `data/` folder (or set up a tiny cron
step to `rsync` the JSON over if backend and frontend live on different
hosts).

## What's still an assumption (tell me and I'll fix it)

1. **Auth style** — currently sends `API_KEY` / `API_SECRET` as custom
   headers (`X-API-KEY` / `X-API-SECRET`). If your API wants a Bearer token
   or Basic auth instead, say so.
2. **Pagination/date-filter params** — assumed `fromDate` / `toDate` /
   `page` / `limit` query params. Adjust to match your actual API docs.
3. **"Closed" status code** — assumed `incidentStatus == 2`. Confirm against
   real data.
4. **SLA breach logic** — currently flags `slaBreached` when `slaStatus` is a
   non-zero/non-null code, or `timeToResolveMinutes` is negative (meaning
   resolved after the SLA due time). Confirm this matches how your platform
   defines a breach.
