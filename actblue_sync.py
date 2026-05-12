"""
ActBlue → Google Sheets Daily Sync
Uses ActBlue's CSV export API (the correct API endpoint).
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
ACTBLUE_CLIENT_UUID   = os.environ["ACTBLUE_CLIENT_UUID"]
ACTBLUE_CLIENT_SECRET = os.environ["ACTBLUE_CLIENT_SECRET"]
GOOGLE_SHEET_ID       = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
DAYS_BACK = int(os.environ.get("DAYS_BACK", "1"))

ACTBLUE_API_BASE = "https://secure.actblue.com/api/v1"
SHEET_NAME = "Contributions"

# ── ActBlue CSV Export API ────────────────────────────────────────────────────

def request_export(date_from: str, date_to: str) -> str:
    """Request a CSV export and return the export ID."""
    auth = (ACTBLUE_CLIENT_UUID, ACTBLUE_CLIENT_SECRET)
    payload = {
        "type": "contributions",
        "date_range_start": date_from,
        "date_range_end": date_to,
    }
    resp = requests.post(
        f"{ACTBLUE_API_BASE}/exports",
        auth=auth,
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    export_id = data["id"]
    print(f"  Export requested: id={export_id}")
    return export_id


def poll_export(export_id: str, max_wait: int = 300) -> str:
    """Poll until the export is ready, then return the download URL."""
    auth = (ACTBLUE_CLIENT_UUID, ACTBLUE_CLIENT_SECRET)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        resp = requests.get(
            f"{ACTBLUE_API_BASE}/exports/{export_id}",
            auth=auth,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status")
        print(f"  Export status: {status}")
        if status == "complete":
            return data["download_url"]
        if status == "failed":
            raise RuntimeError(f"ActBlue export failed: {data}")
        time.sleep(10)
    raise TimeoutError(f"Export {export_id} did not complete within {max_wait}s")


def download_csv(download_url: str) -> list:
    """Download the CSV and return a list of row dicts."""
    resp = requests.get(download_url, timeout=60)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    return list(reader)


def fetch_contributions(date_from: str, date_to: str) -> list:
    export_id = request_export(date_from, date_to)
    download_url = poll_export(export_id)
    rows = download_csv(download_url)
    return rows


# ── Row mapping ───────────────────────────────────────────────────────────────

def contribution_to_row(c: dict) -> list:
    return [
        c.get("Contribution ID") or c.get("contribution_id", ""),
        c.get("Date") or c.get("date", ""),
        c.get("Amount") or c.get("amount", ""),
        c.get("Recurring Amount") or c.get("recurring_amount", ""),
        c.get("Donor First Name") or c.get("donor_firstname", ""),
        c.get("Donor Last Name") or c.get("donor_lastname", ""),
        c.get("Donor Email") or c.get("donor_email", ""),
        c.get("Donor Address 1") or c.get("donor_addr1", ""),
        c.get("Donor City") or c.get("donor_city", ""),
        c.get("Donor State") or c.get("donor_state", ""),
        c.get("Donor ZIP") or c.get("donor_zip", ""),
        c.get("Employer") or c.get("employer", ""),
        c.get("Occupation") or c.get("occupation", ""),
        c.get("Recipient Committee") or c.get("recipient_committee", ""),
        c.get("Fundraising Page") or c.get("fundraising_page", ""),
        c.get("Payment Type") or c.get("payment_type", ""),
        c.get("Mobile") or c.get("is_mobile", ""),
        c.get("Recurring") or c.get("is_recurring", ""),
        c.get("Refunded") or c.get("refunded", ""),
        c.get("Refund Date") or c.get("refund_date", ""),
    ]

HEADERS = [
    "Contribution ID", "Date", "Amount", "Recurring Amount",
    "Donor First Name", "Donor Last Name", "Donor Email",
    "Donor Address", "Donor City", "Donor State", "Donor Zip",
    "Employer", "Occupation",
    "Recipient Committee", "Fundraising Page",
    "Payment Type", "Is Mobile", "Is Recurring",
    "Refunded", "Refund Date",
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
    col = sheet.col_values(1)
    return set(col[1:])


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today = datetime.now(timezone.utc).date()
    date_to   = str(today)
    date_from = str(today - timedelta(days=DAYS_BACK))

    print(f"Fetching ActBlue contributions from {date_from} to {date_to}...")
    contributions = fetch_contributions(date_from, date_to)
    print(f"  -> {len(contributions)} row(s) in export")

    if not contributions:
        print("Nothing to write.")
        return

    print(f"  CSV columns: {list(contributions[0].keys())}")

    sheet = get_sheet()
    ensure_headers(sheet)
    existing_ids = get_existing_ids(sheet)

    id_key = next(
        (k for k in contributions[0].keys() if "id" in k.lower() and "contribution" in k.lower()),
        list(contributions[0].keys())[0]
    )

    new_rows = [
        contribution_to_row(c)
        for c in contributions
        if str(c.get(id_key, "")) not in existing_ids
    ]

    print(f"  -> {len(new_rows)} new row(s) to append ({len(contributions) - len(new_rows)} duplicates skipped)")
    if new_rows:
        sheet.append_rows(new_rows, value_input_option="USER_ENTERED")
    print("Done!")


if __name__ == "__main__":
    main()
