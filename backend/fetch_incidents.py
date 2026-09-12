"""
fetch_incidents.py

Pulls incidents from the Vigilhawk MSSP API for "today" (12:00 AM to 11:59 PM
in the configured timezone), flattens each record into a dashboard-friendly
shape, and writes it to JSON that the React dashboard reads.

It also separately fetches a *pending* window and buckets anything still
open into three named statuses -- Pending with Client / Pending with SOC /
On Hold -- per assignee. This exists because the plain "today" fetch only
returns detections *created* today: a detection opened last week (or last
year) that's still open would otherwise disappear from the dashboard the
moment the day rolls over.

By default that pending window now starts from PENDING_SINCE_DATE (a fixed
calendar date, default "2000-01-01" -- i.e. effectively "from the
beginning") rather than a rolling N-day lookback, so older still-open
tickets never silently drop off just because they're more than a few weeks
old. If you know the actual date you started using this system, set
PENDING_SINCE_DATE in backend/.env to that date -- it'll fetch less data on
every refresh and be faster/lighter on the API. If you'd rather go back to
the old rolling-window behaviour, set PENDING_SINCE_DATE="" (empty) in
.env and PENDING_LOOKBACK_DAYS will be used instead.

RATE LIMITING: Vigilhawk doesn't appear to advertise a rate limit via
response headers (checked in Postman -- no X-RateLimit-* / Retry-After
present under normal conditions). Rather than guess a "safe" polling
interval, fetch_page() below now detects a 429 (Too Many Requests) response
if one ever happens and backs off automatically -- honoring the API's
Retry-After header if it sends one on that response, otherwise an
increasing wait -- then retries, up to MAX_RETRIES times. Same automatic
backoff applies to transient 5xx server errors. This means REFRESH_INTERVAL_
MINUTES (in server.py) can just be left at whatever you want for freshness;
if it's ever too aggressive, this file slows itself down on its own instead
of failing or needing you to manually re-tune anything.

This file does NOT need to be run by hand. server.py imports the functions
below and re-runs this same logic automatically on a schedule (see
REFRESH_INTERVAL_MINUTES in server.py) as long as server.py is left
running (e.g. deployed on Render, or run locally with `python server.py`
and kept alive). Running `python fetch_incidents.py` directly is only for
one-off/manual refreshes or for wiring into your own cron job instead of
using server.py.

Run modes:
    python fetch_incidents.py            -> normal refresh (only needed if
                                             you are NOT running server.py;
                                             e.g. call this every 10-15 min
                                             via cron instead)
    python fetch_incidents.py --eod      -> end-of-day run: does a final
                                             refresh, then:
                                               1) emails the SOC team a list
                                                  of everything currently
                                                  pending (today's items plus
                                                  anything older still open).
                                               2) emails each assignee their
                                                  own personal list of what's
                                                  still open in their name.
    python fetch_incidents.py --inspect-statuses
                                          -> diagnostic: prints the real
                                             status field values from the API
                                             so you can configure
                                             PENDING_STATUS_FIELD / the
                                             STATUS_*_MATCH env vars.

Nothing here should need editing except backend/.env (copy from .env.example).
If your API's auth scheme, pagination style, or date-filter params differ from
what's assumed below, those are isolated in auth_headers() and fetch_page() --
tell me the exact request shape from your API docs/Postman collection and I'll
adjust just those two functions.

Status codes (confirmed from the API): 6 = Pending Client, 7 = Pending SOC,
9 = On Hold, and 0/2/10 = closed (False Positive / True Positive / Resolved).
If your tenant uses different numeric codes for any of these, override
STATUS_PENDING_CLIENT_CODE / STATUS_PENDING_SOC_CODE / STATUS_ON_HOLD_CODE /
CLOSED_STATUS_CODES in backend/.env -- no code changes needed.
"""

import os
import sys
import json
import time
import smtplib
import argparse
from datetime import datetime, timedelta, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
import pytz
from dotenv import load_dotenv

load_dotenv()

API_BASE_URL = os.getenv("API_BASE_URL")
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
API_KEY_HEADER = os.getenv("API_KEY_HEADER", "X-Api-Key")
API_SECRET_HEADER = os.getenv("API_SECRET_HEADER", "X-Api-Secret")
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "100"))

TIMEZONE = pytz.timezone(os.getenv("TIMEZONE", "Asia/Kolkata"))
CLOSED_STATUS_CODE = int(os.getenv("CLOSED_STATUS_CODE", "2"))
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "../frontend/public/data")

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USERNAME)
EMAIL_TO = os.getenv("EMAIL_TO")

# --------------------------------------------------------------------------
# Rate-limit self-defense (see module docstring)
# --------------------------------------------------------------------------
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
# Used only if the API returns 429 WITHOUT a Retry-After header. Multiplied
# by the attempt number, so wait time increases each retry (30s, 60s, 90s...).
DEFAULT_RETRY_AFTER_SECONDS = int(os.getenv("DEFAULT_RETRY_AFTER_SECONDS", "30"))

# --------------------------------------------------------------------------
# Pending window configuration
# --------------------------------------------------------------------------
# PENDING_SINCE_DATE: fixed calendar date (YYYY-MM-DD) the "pending" fetch
# always starts from, so tickets from day one are included -- not just a
# rolling last-N-days window. Defaults to "2000-01-01", which in practice
# means "everything the API has". Set this in backend/.env to your actual
# go-live date once you know it, so every refresh pulls less data and is
# faster and lighter on the API.
#
# To go back to the OLD behaviour (rolling N-day window instead of a fixed
# start date), set PENDING_SINCE_DATE="" (empty string) in .env -- then
# PENDING_LOOKBACK_DAYS is used instead, same as before.
PENDING_SINCE_DATE = os.getenv("PENDING_SINCE_DATE", "2000-01-01").strip()

# Only used as a fallback if PENDING_SINCE_DATE is explicitly set to "" in .env.
PENDING_LOOKBACK_DAYS = int(os.getenv("PENDING_LOOKBACK_DAYS", "60"))

# --------------------------------------------------------------------------
# Status IDs (confirmed from the API/Postman -- this is the source of truth,
# not a custom field's free text):
#   1 Created | 3 Assigned | 4 Acknowledged | 5 In Progress | 6 Pending Client
#   7 Pending SOC | 8 Blocked | 9 On Hold | 10 Resolved
#   0 Closed - False Positive | 2 Closed - True Positive
# --------------------------------------------------------------------------
STATUS_LABELS = {
    1: "Created",
    3: "Assigned",
    4: "Acknowledged",
    5: "In Progress",
    6: "Pending Client",
    7: "Pending SOC",
    8: "Blocked",
    9: "On Hold",
    10: "Resolved",
    0: "Closed - False Positive",
    2: "Closed - True Positive",
}

STATUS_PENDING_CLIENT_CODE = int(os.getenv("STATUS_PENDING_CLIENT_CODE", "6"))
STATUS_PENDING_SOC_CODE = int(os.getenv("STATUS_PENDING_SOC_CODE", "7"))
STATUS_ON_HOLD_CODE = int(os.getenv("STATUS_ON_HOLD_CODE", "9"))

# Which status codes count as "closed" (excluded from every open/pending
# bucket). Resolved (10) is included here by default on the assumption that
# a resolved ticket doesn't need further SOC/client action -- set
# CLOSED_STATUS_CODES=0,2 in .env if you want Resolved to still show as open
# until it's formally closed.
CLOSED_STATUS_CODES = {
    int(code.strip()) for code in os.getenv("CLOSED_STATUS_CODES", "0,2,10").split(",") if code.strip() != ""
}


# --------------------------------------------------------------------------
# 1. Auth + fetch
# --------------------------------------------------------------------------

def auth_headers():
    """Header-based API key/secret auth. Swap this out if your API instead
    wants `Authorization: Bearer <token>` or HTTP Basic auth."""
    return {
        API_KEY_HEADER: API_KEY,
        API_SECRET_HEADER: API_SECRET,
        "Content-Type": "application/json",
    }


def day_bounds_str(target_date):
    """Return (startDate, endDate) as plain YYYY-MM-DD strings covering all of
    target_date. endDate is the *next* calendar day, matching how the API's
    own Postman example scopes a single day (startDate=2026-07-01,
    endDate=2026-07-02 -> returns the 1st)."""
    return target_date.isoformat(), (target_date + timedelta(days=1)).isoformat()


def fetch_page(start_date, end_date, page_index, page_size=PAGE_SIZE, _attempt=1):
    """Fetch a single page of incidents. Matches the confirmed Postman
    request: POST with a JSON body.

    Self-defending against rate limits: if the API responds 429 (Too Many
    Requests), this waits and retries instead of failing outright -- using
    the Retry-After response header if the API sends one, otherwise an
    increasing backoff (DEFAULT_RETRY_AFTER_SECONDS x attempt number). Same
    treatment for transient 5xx server errors. Gives up after MAX_RETRIES
    attempts and raises normally, so a genuinely broken request still
    surfaces instead of retrying forever.
    """
    payload = {
        "pageIndex": page_index,
        "pageSize": page_size,
        "startDate": start_date,
        "endDate": end_date,
    }
    resp = requests.post(API_BASE_URL, headers=auth_headers(), json=payload, timeout=30)

    if resp.status_code == 429 and _attempt <= MAX_RETRIES:
        retry_after = resp.headers.get("Retry-After")
        try:
            wait_seconds = int(retry_after) if retry_after else DEFAULT_RETRY_AFTER_SECONDS * _attempt
        except ValueError:
            wait_seconds = DEFAULT_RETRY_AFTER_SECONDS * _attempt
        print(f"Rate limited (429) on page {page_index} -- waiting {wait_seconds}s before retry "
              f"{_attempt}/{MAX_RETRIES}...")
        time.sleep(wait_seconds)
        return fetch_page(start_date, end_date, page_index, page_size, _attempt=_attempt + 1)

    if resp.status_code >= 500 and _attempt <= MAX_RETRIES:
        wait_seconds = min(60, 5 * _attempt)
        print(f"API returned {resp.status_code} on page {page_index} -- retrying in {wait_seconds}s "
              f"({_attempt}/{MAX_RETRIES})...")
        time.sleep(wait_seconds)
        return fetch_page(start_date, end_date, page_index, page_size, _attempt=_attempt + 1)

    if not resp.ok:
        print(f"API returned {resp.status_code} for payload {payload}")
        print(f"Response body: {resp.text}")
    resp.raise_for_status()
    data = resp.json()
    # Assumes the shape shown in your sample: {"success": true, "data": {"incidents": [...]}}
    return data.get("data", {}).get("incidents", [])


def fetch_all_pages(start_date, end_date):
    """Page through every incident in [start_date, end_date)."""
    all_incidents = []
    page_index = 1
    while True:
        batch = fetch_page(start_date, end_date, page_index)
        if not batch:
            break
        all_incidents.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        page_index += 1
    return all_incidents


def fetch_today_incidents():
    today = datetime.now(TIMEZONE).date()
    start_date, end_date = day_bounds_str(today)
    return today, fetch_all_pages(start_date, end_date)


def _pending_window_start(today):
    """Decide where the pending window starts: a fixed calendar date
    (PENDING_SINCE_DATE, the new default -- 'from the beginning') or, only
    if that's explicitly turned off, a rolling N-day lookback like before."""
    if PENDING_SINCE_DATE:
        # Validate the format early so a typo in .env fails loudly here
        # instead of producing a confusing API error later.
        try:
            date.fromisoformat(PENDING_SINCE_DATE)
        except ValueError:
            raise ValueError(
                f"PENDING_SINCE_DATE={PENDING_SINCE_DATE!r} in .env is not a valid "
                "YYYY-MM-DD date."
            )
        return PENDING_SINCE_DATE
    return (today - timedelta(days=PENDING_LOOKBACK_DAYS)).isoformat()


def fetch_pending_window_incidents():
    """Fetch and flatten every incident created since the pending window's
    start (see _pending_window_start) up to and including today. This is
    the wider window used to catch anything still pending from before
    today -- see module docstring."""
    today = datetime.now(TIMEZONE).date()
    window_start = _pending_window_start(today)
    _, window_end = day_bounds_str(today)  # tomorrow's date, as the API expects

    raw = fetch_all_pages(window_start, window_end)
    return [flatten_incident(inc) for inc in raw]


# --------------------------------------------------------------------------
# 2. Flatten
# --------------------------------------------------------------------------

def flatten_incident(inc):
    """Collapse the nested customFields array into flat cf_<fieldName> keys
    and pull out only what the dashboard needs."""
    flat = {
        "id": inc.get("incidentId"),
        "title": inc.get("title"),
        "clientName": inc.get("tenantName"),
        "source": inc.get("source"),
        "severityId": inc.get("severityId"),
        "severityName": inc.get("severityName"),
        "incidentStatusCode": inc.get("incidentStatus"),
        "assigneeName": inc.get("assigneeName"),
        "assigneeEmail": inc.get("assigneeEmail"),
        "detectionDateTime": inc.get("detectionDateTime"),
        "createdAt": inc.get("createdAt"),
        "updatedAt": inc.get("updatedAt"),
        "resolutionDateTime": inc.get("resolutionDateTime"),
        "slaDueDateTime": inc.get("slaDueDateTime"),
        "slaStatusCode": inc.get("slaStatus"),
        "timeToResolveMinutes": inc.get("timeToResolveMinutes"),
        "description": inc.get("description"),
        "externalIncidentUrl": inc.get("externalIncidentUrl"),
    }

    for cf in inc.get("customFields", []):
        field_def = cf.get("customFieldObjId", {})
        name = field_def.get("fieldName")
        if name:
            flat[f"cf_{name}"] = cf.get("fieldValue")

    flat["statusLabel"] = STATUS_LABELS.get(flat["incidentStatusCode"], "Unknown")
    flat["isClosed"] = flat["incidentStatusCode"] in CLOSED_STATUS_CODES

    sla_label, is_breached = compute_sla_status(flat)
    flat["slaStatusLabel"] = sla_label
    flat["slaBreached"] = is_breached

    return flat


# --------------------------------------------------------------------------
# SLA status mapping: 1 = Pending, 2 = Met, 3 = Breached (confirmed values).
# If your tenant uses different numbers for any of these, override via
# SLA_STATUS_PENDING_CODE / SLA_STATUS_MET_CODE / SLA_STATUS_BREACHED_CODE
# in backend/.env -- no code changes needed.
# --------------------------------------------------------------------------
SLA_STATUS_PENDING_CODE = int(os.getenv("SLA_STATUS_PENDING_CODE", "1"))
SLA_STATUS_MET_CODE = int(os.getenv("SLA_STATUS_MET_CODE", "2"))
SLA_STATUS_BREACHED_CODE = int(os.getenv("SLA_STATUS_BREACHED_CODE", "3"))

SLA_STATUS_LABELS = {
    SLA_STATUS_PENDING_CODE: "Pending",
    SLA_STATUS_MET_CODE: "Met",
    SLA_STATUS_BREACHED_CODE: "Breached",
}


def compute_sla_status(flat):
    """Returns (slaStatusLabel: str, isBreached: bool) purely from
    slaStatusCode -- 1 = Pending, 2 = Met, 3 = Breached.

    Deliberately NOT using slaDueDateTime/resolutionDateTime math anymore --
    this tenant's slaStatusCode is the source of truth per confirmed
    mapping. Any code that isn't 1/2/3 (e.g. 0, or anything else not yet
    seen) comes back as "Unknown" rather than silently guessing. If
    "Unknown" shows up a lot on the dashboard, that's a sign there's a code
    in play that isn't in this mapping -- tell me the code and what it
    means and I'll add it.
    """
    code = flat.get("slaStatusCode")
    label = SLA_STATUS_LABELS.get(code, "Unknown")
    is_breached = code == SLA_STATUS_BREACHED_CODE
    return label, is_breached


# --------------------------------------------------------------------------
# 3. Save
# --------------------------------------------------------------------------

def save_json(today, incidents):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    dated_path = os.path.join(OUTPUT_DIR, f"incidents_{today.isoformat()}.json")
    latest_path = os.path.join(OUTPUT_DIR, "incidents_latest.json")

    payload = {
        "date": today.isoformat(),
        "generatedAt": datetime.now(TIMEZONE).isoformat(),
        "count": len(incidents),
        "incidents": incidents,
    }

    for path in (dated_path, latest_path):
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, default=str)

    print(f"Saved {len(incidents)} incidents -> {dated_path}")


def save_pending_json(summary):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "pending_latest.json")
    with open(path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"Saved pending summary -> {path} (totals: {summary['totals']})")


# --------------------------------------------------------------------------
# 4. Pending-status bucketing (Pending with Client / Pending with SOC / On Hold)
# --------------------------------------------------------------------------

def categorize_pending_status(flat):
    """Return 'pendingWithClient', 'pendingWithSoc', 'onHold', or None,
    based on the incident's numeric status code (see STATUS_LABELS above).

    None means either the incident is closed, or its status is one of the
    other codes (Created/Assigned/Acknowledged/In Progress/Blocked) --
    those still show up elsewhere on the dashboard, just not in these three
    cards.
    """
    if flat.get("isClosed"):
        return None
    code = flat.get("incidentStatusCode")
    if code == STATUS_PENDING_CLIENT_CODE:
        return "pendingWithClient"
    if code == STATUS_PENDING_SOC_CODE:
        return "pendingWithSoc"
    if code == STATUS_ON_HOLD_CODE:
        return "onHold"
    return None


def build_pending_summary(flattened_incidents):
    """Totals across all three buckets, plus a per-assignee breakdown,
    sorted with the busiest assignee first."""
    totals = {"pendingWithClient": 0, "pendingWithSoc": 0, "onHold": 0}
    by_assignee = {}

    for flat in flattened_incidents:
        bucket = categorize_pending_status(flat)
        if not bucket:
            continue
        totals[bucket] += 1
        name = flat.get("assigneeName") or "Unassigned"
        row = by_assignee.setdefault(
            name, {"name": name, "pendingWithClient": 0, "pendingWithSoc": 0, "onHold": 0}
        )
        row[bucket] += 1

    by_assignee_list = sorted(
        by_assignee.values(),
        key=lambda r: (r["pendingWithClient"] + r["pendingWithSoc"] + r["onHold"]),
        reverse=True,
    )

    return {
        "generatedAt": datetime.now(TIMEZONE).isoformat(),
        # Only one of these two will be meaningful, depending on whether
        # PENDING_SINCE_DATE is set -- the frontend checks sinceDate first.
        "sinceDate": PENDING_SINCE_DATE or None,
        "lookbackDays": None if PENDING_SINCE_DATE else PENDING_LOOKBACK_DAYS,
        "totals": totals,
        "byAssignee": by_assignee_list,
    }


# --------------------------------------------------------------------------
# 5. End-of-day escalation emails
# --------------------------------------------------------------------------

def _open_incident_rows_html(incidents, include_assignee=True):
    """Shared row-builder for the escalation email tables."""
    return "".join(
        (
            "<tr>"
            f"<td style='padding:6px 10px;border:1px solid #ddd;'>{i.get('id')}</td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd;'>{i.get('clientName')}</td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd;'>{i.get('severityName')}</td>"
            + (f"<td style='padding:6px 10px;border:1px solid #ddd;'>{i.get('assigneeName')}</td>" if include_assignee else "")
            + f"<td style='padding:6px 10px;border:1px solid #ddd;'>{'Breached' if i.get('slaBreached') else 'On track'}</td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd;'>{i.get('title')}</td>"
            "</tr>"
        )
        for i in incidents
    )


def send_open_incident_email(today, open_incidents):
    """One summary email to the whole SOC team listing everything currently
    pending -- today's items plus anything older still open."""
    if not open_incidents:
        print("No open incidents pending. No SOC summary email sent.")
        return
    if not (SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD and EMAIL_TO):
        print("SMTP not configured (check backend/.env) -- skipping SOC summary email.")
        return

    subject = f"[SOC] {len(open_incidents)} incident(s) still open as of end of day {today.isoformat()}"
    rows = _open_incident_rows_html(open_incidents, include_assignee=True)

    html_body = f"""
    <p>The following incident(s) were not closed before 11:59 PM on {today.isoformat()}
    (includes anything opened earlier that's still pending). Please review and follow up first thing.</p>
    <table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px;">
      <tr style="background:#f2f2f2;">
        <th style='padding:6px 10px;border:1px solid #ddd;text-align:left;'>Incident ID</th>
        <th style='padding:6px 10px;border:1px solid #ddd;text-align:left;'>Client</th>
        <th style='padding:6px 10px;border:1px solid #ddd;text-align:left;'>Severity</th>
        <th style='padding:6px 10px;border:1px solid #ddd;text-align:left;'>Assignee</th>
        <th style='padding:6px 10px;border:1px solid #ddd;text-align:left;'>SLA</th>
        <th style='padding:6px 10px;border:1px solid #ddd;text-align:left;'>Title</th>
      </tr>
      {rows}
    </table>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(EMAIL_FROM, EMAIL_TO.split(","), msg.as_string())

    print(f"Escalation email sent to {EMAIL_TO} ({len(open_incidents)} open incident(s)).")


def _group_open_incidents_by_assignee(open_incidents):
    """Group open incidents by assignee email. Incidents with no assignee
    email on file are dropped here (there's nowhere to send them) -- they're
    still covered by the SOC summary email above."""
    grouped = {}
    skipped = 0
    for inc in open_incidents:
        email = (inc.get("assigneeEmail") or "").strip()
        if not email:
            skipped += 1
            continue
        grouped.setdefault(email, {"name": inc.get("assigneeName") or "there", "incidents": []})
        grouped[email]["incidents"].append(inc)
    if skipped:
        print(f"{skipped} open incident(s) have no assigneeEmail on file -- skipped for personal reminders.")
    return grouped


def send_assignee_reminder_emails(today, open_incidents):
    """Send each assignee a personal email listing only the incidents
    assigned to them that are currently pending (today's plus older)."""
    if not open_incidents:
        print("No open incidents pending. No assignee reminder emails sent.")
        return
    if not (SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD):
        print("SMTP not configured (check backend/.env) -- skipping assignee reminder emails.")
        return

    grouped = _group_open_incidents_by_assignee(open_incidents)
    if not grouped:
        print("No open incidents have an assignee email on file -- no reminders to send.")
        return

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)

        for email, info in grouped.items():
            incidents = info["incidents"]
            subject = f"[SOC] {len(incidents)} of your incident(s) currently pending as of {today.isoformat()}"
            rows = _open_incident_rows_html(incidents, include_assignee=False)

            html_body = f"""
            <p>Hi {info['name']},</p>
            <p>The following incident(s) assigned to you are still open as of end of day
            {today.isoformat()} (includes anything opened earlier that's still pending).
            Please review and follow up first thing.</p>
            <table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px;">
              <tr style="background:#f2f2f2;">
                <th style='padding:6px 10px;border:1px solid #ddd;text-align:left;'>Incident ID</th>
                <th style='padding:6px 10px;border:1px solid #ddd;text-align:left;'>Client</th>
                <th style='padding:6px 10px;border:1px solid #ddd;text-align:left;'>Severity</th>
                <th style='padding:6px 10px;border:1px solid #ddd;text-align:left;'>SLA</th>
                <th style='padding:6px 10px;border:1px solid #ddd;text-align:left;'>Title</th>
              </tr>
              {rows}
            </table>
            """

            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = EMAIL_FROM
            msg["To"] = email
            msg.attach(MIMEText(html_body, "html"))

            server.sendmail(EMAIL_FROM, [email], msg.as_string())
            print(f"Reminder email sent to {email} ({len(incidents)} open incident(s)).")


# --------------------------------------------------------------------------
# 6. Diagnostics
# --------------------------------------------------------------------------

def inspect_pending_statuses():
    """Run: python fetch_incidents.py --inspect-statuses

    Prints every value found for incidentStatusCode (plus slaStatusCode)
    across all currently-open incidents in the pending window, with counts.
    Use this to sanity-check that STATUS_PENDING_CLIENT_CODE /
    STATUS_PENDING_SOC_CODE / STATUS_ON_HOLD_CODE / CLOSED_STATUS_CODES in
    .env actually match what the API is sending back.
    """
    flattened = fetch_pending_window_incidents()
    open_only = [f for f in flattened if not f["isClosed"]]
    window_desc = f"since {PENDING_SINCE_DATE}" if PENDING_SINCE_DATE else f"in the last {PENDING_LOOKBACK_DAYS} day(s)"
    print(f"\nFetched {len(flattened)} incident(s) {window_desc}; "
          f"{len(open_only)} are not closed.\n")

    if not open_only:
        print("No open incidents in this window -- nothing to inspect. "
              "If you expected some, double-check PENDING_SINCE_DATE (or "
              "PENDING_LOOKBACK_DAYS) and CLOSED_STATUS_CODES.")
        return

    counts = {}
    for f in open_only:
        code = f.get("incidentStatusCode")
        counts[code] = counts.get(code, 0) + 1

    print("--- incidentStatusCode (open incidents only) ---")
    for code, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        label = STATUS_LABELS.get(code, "Unknown")
        print(f"  {count:>4}  code={code!r:>4}  ({label})")
    print()

    sla_counts = {}
    for f in flattened:  # SLA status is meaningful on closed incidents too, so use everything, not just open_only
        code = f.get("slaStatusCode")
        sla_counts[code] = sla_counts.get(code, 0) + 1

    print("--- slaStatusCode (ALL incidents in this window, open + closed) ---")
    for code, count in sorted(sla_counts.items(), key=lambda kv: -kv[1]):
        label = SLA_STATUS_LABELS.get(code, "Unknown")
        print(f"  {count:>4}  code={code!r:>4}  ({label})")
    print()

    print("Currently configured (backend/.env):")
    print(f"  PENDING_SINCE_DATE        = {PENDING_SINCE_DATE!r}")
    print(f"  PENDING_LOOKBACK_DAYS     = {PENDING_LOOKBACK_DAYS!r} (only used if PENDING_SINCE_DATE is empty)")
    print(f"  STATUS_PENDING_CLIENT_CODE = {STATUS_PENDING_CLIENT_CODE!r}")
    print(f"  STATUS_PENDING_SOC_CODE    = {STATUS_PENDING_SOC_CODE!r}")
    print(f"  STATUS_ON_HOLD_CODE        = {STATUS_ON_HOLD_CODE!r}")
    print(f"  CLOSED_STATUS_CODES        = {sorted(CLOSED_STATUS_CODES)!r}")
    print(f"  SLA_STATUS_PENDING_CODE    = {SLA_STATUS_PENDING_CODE!r}")
    print(f"  SLA_STATUS_MET_CODE        = {SLA_STATUS_MET_CODE!r}")
    print(f"  SLA_STATUS_BREACHED_CODE   = {SLA_STATUS_BREACHED_CODE!r}")
    print("\nIf any code above doesn't match STATUS_LABELS, or a code you expected to see as "
          "pending/closed is missing from the right list, update the matching *_CODE / "
          "CLOSED_STATUS_CODES env var in backend/.env.")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--eod", action="store_true", help="Run end-of-day check and send escalation emails for anything still open.")
    parser.add_argument("--inspect-statuses", action="store_true",
                         help="Print real status codes/values from the API to help sanity-check the "
                              "STATUS_*_CODE and CLOSED_STATUS_CODES env vars, then exit.")
    args = parser.parse_args()

    if not API_BASE_URL or not API_KEY:
        print("Missing API_BASE_URL / API_KEY. Copy .env.example to .env and fill it in.", file=sys.stderr)
        sys.exit(1)

    if args.inspect_statuses:
        inspect_pending_statuses()
        return

    today, raw_incidents = fetch_today_incidents()
    flattened = [flatten_incident(inc) for inc in raw_incidents]
    save_json(today, flattened)

    # Wider window (from PENDING_SINCE_DATE by default) so pending
    # detections from before today don't disappear.
    pending_window_flattened = fetch_pending_window_incidents()
    pending_summary = build_pending_summary(pending_window_flattened)
    save_pending_json(pending_summary)

    if args.eod:
        open_incidents_all = [i for i in pending_window_flattened if not i["isClosed"]]
        send_open_incident_email(today, open_incidents_all)
        send_assignee_reminder_emails(today, open_incidents_all)


if __name__ == "__main__":
    main()