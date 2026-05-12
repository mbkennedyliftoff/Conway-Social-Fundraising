"""
ActBlue -> Google Sheets Daily Sync
Uses ActBlue's CSV Download API:
  POST https://secure.actblue.com/api/v1/csvs
  GET  https://secure.actblue.com/api/v1/csvs/{id}  (poll until ready)
  Download the CSV from the returned URL
"""

import csv
import io
import json
import os
import time
from datetime import datetime, timedelta, timezone

import gspread
import requests
from google.oauth2.service_account import Credentials

# ── Config ────────────────────────────────────────────────────────────────────
ACTBLUE_CLIENT_UUID     = os.environ["ACTBLUE_CLIENT_UUID"]
ACTBLUE_CLIENT_SECRET   = os.environ["ACTBLUE_CLIENT_SECRET"]
GOOGLE_SHEET_ID         = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
DAYS_BACK = int(os.environ.get("DAYS_BACK", "1"))

ACTBLUE_API_BASE = "https://secure.actblue.com/api/v1"
SHEET_NAME = "Contributions"

HEADERS = [
    "Contribution ID", "Date", "Amount", "Recurring Amount",
    "Donor First Name", "Donor Last Name", "Donor Email",
    "Donor Address", "Donor City", "Donor State", "Donor Zip",
    "Employer", "Occupation",
    "Recipient Committee", "Fundraising Page",
    "Payment Type", "Is Mobile", "Is Recurring",
    "Refunded", "Refund Date",
]

# ── ActBlue CSV Download API ──────────────────────────────────────────────────

def request_csv(date_from: str, date_to: str) -> str:
    """Request a CSV download and return the CSV ID."""
    auth = (ACTBLUE_CLIENT_UUID, ACTBLUE_CLIENT_SECRET)
    payload = {
        "csv_type": "contributions",
        "date_range_start": date_from,
        "date_range_end": date_to,
    }
    resp = requests.post(
        f"{ACTBLUE_API_BASE}/csvs",
        auth=auth,
        json=payload,
        timeout=30,
    )
    print(f"  POST /csvs -> status {resp.status_code}")
    if not resp.ok:
        print(f"  Response body: {resp.text}")
    resp.raise_for_status()
    data = resp.json()
    print(f"  Response: {data}")
    csv_id = data.get("id") or data.get("csv_id")
    return str(csv_id)


def poll_csv(csv_id: str, max_wait: int = 300) -> str:
    """Poll until the CSV is ready, return its download URL."""
    auth = (ACTBLUE_CLIENT_UUID, ACTBLUE_CLIENT_SECRET)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        resp = requests.get(
            f"{ACTBLUE_API_BASE}/csvs/{csv_id}",
            auth=auth,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status")
        print(f"  CSV status: {status}")
        if status == "complete":
            return data.get("download_url") or data.get("url")
        if status in ("failed", "error"):
            raise RuntimeError(f"ActBlue CSV generation failed: {data}")
        time.sleep(10)
    raise TimeoutError(f"CSV {csv_id} did not complete within {max_wait}s")


def download_csv(url: str) -> list:
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    return list(reader)


def fetch_contributions(date_from: str, date_to: str) -> list:
    csv_id = request_csv(date_from, date_to)
    download_url = poll_csv(csv_id)
    return download_csv(download_url)


# ── Row mapping ───────────────────────────────────────────────────────────────

def get(row: dict, *keys) -> str:
    for k in keys:
        v = row.get(k, "")
        if v:
            return v
    return ""

def contribution_to_row(c: dict) -> list:
    return [
        get(c, "Contribution ID", "contribution_id", "id"),
        get(c, "Date", "date", "created_at"),
        get(c, "Amount", "amount"),
        get(c, "Recurring Amount", "recurring_amount"),
        get(c, "Donor First Name", "donor_firstname", "firstname"),
        get(c, "Donor Last Name", "donor_lastname", "lastname"),
        get(c, "Donor Email", "donor_email", "email"),
        get(c, "Donor Address 1", "donor_addr1", "addr1"),
        get(c, "Donor City", "donor_city", "city"),
        get(c, "Donor State", "donor_state", "state"),
        get(c, "Donor ZIP", "donor_zip", "zip"),
        get(c, "Employer", "employer"),
        get(c, "Occupation", "occupation"),
        get(c, "Recipient Committee", "recipient_committee"),
        get(c, "Fundraising Page", "fundraising_page"),
        get(c, "Payment Type", "payment_type"),
        get(c, "Mobile", "is_mobile"),
        get(c, "Recurring", "is_recurring"),
        get(c, "Refunded", "refunded"),
        get(c, "Refund Date", "refund_date"),
    ]


# ── Google Sheets ─────────────────────────────────────────────────────────────

def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    try:
        sheet = spreadsheet.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=SHEET_NAME, rows=10000, cols=len(HEADERS))
    return sheet


def ensure_headers(sheet):
    if sheet.row_values(1) != HEADERS:
        sheet.update("A1", [HEADERS])
        sheet.format("A1:T1", {"textFormat": {"bold": True}})


def get_existing_ids(sheet) -> set:
    return set(sheet.col_values(1)[1:])


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today     = datetime.now(timezone.utc).date()
    date_to   = str(today)
    date_from = str(today - timedelta(days=DAYS_BACK))

    print(f"Fetching ActBlue contributions from {date_from} to {date_to}...")
    contributions = fetch_contributions(date_from, date_to)
    print(f"  -> {len(contributions)} row(s) in CSV")

    if not contributions:
        print("Nothing to write.")
        return

    print(f"  CSV columns: {list(contributions[0].keys())}")

    sheet = get_sheet()
    ensure_headers(sheet)
    existing_ids = get_existing_ids(sheet)

    # Find the ID column name dynamically
    id_key = next(
        (k for k in contributions[0].keys()
         if "contribution" in k.lower() and "id" in k.lower()),
        list(contributions[0].keys())[0],
    )

    new_rows = [
        contribution_to_row(c)
        for c in contributions
        if str(c.get(id_key, "")) not in existing_ids
    ]

    print(f"  -> {len(new_rows)} new rows to append ({len(contributions)-len(new_rows)} skipped as duplicates)")
    if new_rows:
        sheet.append_rows(new_rows, value_input_option="USER_ENTERED")
    print("Done!")


if __name__ == "__main__":
    main()
