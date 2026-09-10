"""
fetch_incidents.py

Pulls incidents from the Vigilhawk MSSP API for "today" (12:00 AM to 11:59 PM
in the configured timezone), flattens each record into a dashboard-friendly
shape, and writes it to JSON that the React dashboard reads.

Run modes:
    python fetch_incidents.py            -> normal refresh (call this every
                                             10-15 min during the day via cron)
    python fetch_incidents.py --eod      -> end-of-day run: does a final
                                             refresh, then emails the SOC team
                                             a list of anything not yet closed
                                             before the day rolls over.

Nothing here should need editing except backend/.env (copy from .env.example).
If your API's auth scheme, pagination style, or date-filter params differ from
what's assumed below, those are isolated in auth_headers() and fetch_page() --
tell me the exact request shape from your API docs/Postman collection and I'll
adjust just those two functions.
"""

import os
import sys
import json
import smtplib
import argparse
from datetime import datetime, timedelta
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


def fetch_page(start_date, end_date, page_index, page_size=PAGE_SIZE):
    """Fetch a single page of incidents. Matches the confirmed Postman
    request: POST with a JSON body."""
    payload = {
        "pageIndex": page_index,
        "pageSize": page_size,
        "startDate": start_date,
        "endDate": end_date,
    }
    resp = requests.post(API_BASE_URL, headers=auth_headers(), json=payload, timeout=30)
    if not resp.ok:
        print(f"API returned {resp.status_code} for payload {payload}")
        print(f"Response body: {resp.text}")
    resp.raise_for_status()
    data = resp.json()
    # Assumes the shape shown in your sample: {"success": true, "data": {"incidents": [...]}}
    return data.get("data", {}).get("incidents", [])


def fetch_today_incidents():
    today = datetime.now(TIMEZONE).date()
    start_date, end_date = day_bounds_str(today)

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

    return today, all_incidents


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

    flat["isClosed"] = flat["incidentStatusCode"] == CLOSED_STATUS_CODE or bool(flat["resolutionDateTime"])
    flat["slaBreached"] = bool(flat.get("slaStatusCode")) and flat["slaStatusCode"] not in (0, None) or (
        flat["timeToResolveMinutes"] is not None and flat["timeToResolveMinutes"] < 0
    )

    return flat


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


# --------------------------------------------------------------------------
# 4. End-of-day escalation email
# --------------------------------------------------------------------------

def send_open_incident_email(today, open_incidents):
    if not open_incidents:
        print("No open incidents at end of day. No email sent.")
        return
    if not (SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD and EMAIL_TO):
        print("SMTP not configured (check backend/.env) -- skipping email send.")
        return

    subject = f"[SOC] {len(open_incidents)} incident(s) still open at end of day {today.isoformat()}"

    rows = "".join(
        f"<tr>"
        f"<td style='padding:6px 10px;border:1px solid #ddd;'>{i.get('id')}</td>"
        f"<td style='padding:6px 10px;border:1px solid #ddd;'>{i.get('clientName')}</td>"
        f"<td style='padding:6px 10px;border:1px solid #ddd;'>{i.get('severityName')}</td>"
        f"<td style='padding:6px 10px;border:1px solid #ddd;'>{i.get('assigneeName')}</td>"
        f"<td style='padding:6px 10px;border:1px solid #ddd;'>{'Breached' if i.get('slaBreached') else 'On track'}</td>"
        f"<td style='padding:6px 10px;border:1px solid #ddd;'>{i.get('title')}</td>"
        f"</tr>"
        for i in open_incidents
    )

    html_body = f"""
    <p>The following incident(s) from {today.isoformat()} were not closed before 11:59 PM.
    Please review and follow up first thing.</p>
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


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--eod", action="store_true", help="Run end-of-day check and send escalation email for anything still open.")
    args = parser.parse_args()

    if not API_BASE_URL or not API_KEY:
        print("Missing API_BASE_URL / API_KEY. Copy .env.example to .env and fill it in.", file=sys.stderr)
        sys.exit(1)

    today, raw_incidents = fetch_today_incidents()
    flattened = [flatten_incident(inc) for inc in raw_incidents]
    save_json(today, flattened)

    if args.eod:
        open_incidents = [i for i in flattened if not i["isClosed"]]
        send_open_incident_email(today, open_incidents)


if __name__ == "__main__":
    main()
