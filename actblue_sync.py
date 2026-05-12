"""
ActBlue → Google Sheets Daily Sync
Pulls contributions from the ActBlue API and writes them to a Google Sheet.
"""

import os
import json
import requests
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials

# ── Config (set via environment variables) ────────────────────────────────────
ACTBLUE_CLIENT_UUID = os.environ["ACTBLUE_CLIENT_UUID"]
ACTBLUE_CLIENT_SECRET = os.environ["ACTBLUE_CLIENT_SECRET"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]           # from the Sheet URL
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]  # service account JSON string

# How many days back to fetch (1 = yesterday only, increase to backfill)
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


# ── ActBlue API ───────────────────────────────────────────────────────────────

def fetch_contributions(date_from: str, date_to: str) -> list[dict]:
    """Fetch all contributions in a date range, handling pagination."""
    auth = (ACTBLUE_CLIENT_UUID, ACTBLUE_CLIENT_SECRET)
    params = {
        "date_range_start": date_from,
        "date_range_end": date_to,
        "limit": 100,
        "offset": 0,
    }
    all_contributions = []

    while True:
        resp = requests.get(
            f"{ACTBLUE_API_BASE}/contributions",
            auth=auth,
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        contributions = data.get("contributions", [])
        all_contributions.extend(contributions)

        # Paginate until we have everything
        total = data.get("total_contributions", 0)
        params["offset"] += len(contributions)
        if params["offset"] >= total or not contributions:
            break

    return all_contributions


def contribution_to_row(c: dict) -> list:
    """Flatten a contribution dict into a sheet row."""
    donor = c.get("donor", {})
    lineitems = c.get("lineitems", [{}])
    li = lineitems[0] if lineitems else {}

    refund = c.get("refund", {})
    refund_date = refund.get("date", "") if refund else ""

    return [
        c.get("id", ""),
        c.get("created_at", "")[:10] if c.get("created_at") else "",
        c.get("amount", ""),
        c.get("recurring_amount", ""),
        donor.get("firstname", ""),
        donor.get("lastname", ""),
        donor.get("email", ""),
        donor.get("addr1", ""),
        donor.get("city", ""),
        donor.get("state", ""),
        donor.get("zip", ""),
        donor.get("employer", ""),
        donor.get("occupation", ""),
        li.get("recipient_committee", ""),
        c.get("fundraising_page", {}).get("name", "") if c.get("fundraising_page") else "",
        c.get("payment_type", ""),
        c.get("is_mobile", ""),
        c.get("is_recurring", ""),
        bool(refund),
        refund_date,
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

    # Get or create the worksheet
    try:
        sheet = spreadsheet.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=SHEET_NAME, rows=10000, cols=len(HEADERS))

    return sheet


def ensure_headers(sheet):
    existing = sheet.row_values(1)
    if existing != HEADERS:
        sheet.update("A1", [HEADERS])
        # Bold the header row
        sheet.format("A1:T1", {"textFormat": {"bold": True}})


def get_existing_ids(sheet) -> set[str]:
    """Read all contribution IDs already in the sheet to avoid duplicates."""
    col = sheet.col_values(1)  # Column A = Contribution ID
    return set(col[1:])  # skip header


def append_rows(sheet, rows: list[list]):
    if not rows:
        return
    sheet.append_rows(rows, value_input_option="USER_ENTERED")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today = datetime.utcnow().date()
    date_to = str(today)
    date_from = str(today - timedelta(days=DAYS_BACK))

    print(f"Fetching ActBlue contributions from {date_from} to {date_to}…")
    contributions = fetch_contributions(date_from, date_to)
    print(f"  → {len(contributions)} contribution(s) returned")

    if not contributions:
        print("Nothing to write.")
        return

    sheet = get_sheet()
    ensure_headers(sheet)
    existing_ids = get_existing_ids(sheet)

    new_rows = [
        contribution_to_row(c)
        for c in contributions
        if str(c.get("id", "")) not in existing_ids
    ]

    print(f"  → {len(new_rows)} new row(s) to append (skipping {len(contributions) - len(new_rows)} duplicates)")
    append_rows(sheet, new_rows)
    print("Done ✓")


if __name__ == "__main__":
    main()
