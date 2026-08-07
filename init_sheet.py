"""
One-time setup script.

What it does:
1. Connects to your Google Sheet using the same service account credentials
   the web app will use.
2. Creates the base tabs (TAs, Students) if they don't already exist, and
   writes the correct header row into each. (Session tabs are NOT created
   here -- the app creates one automatically per date the first time a TA
   opens/saves that day, named e.g. "Lab - 2026-08-04".)
3. Optionally imports your existing TA list and Student list from CSV
   files, so you don't have to retype everything by hand.

USAGE
-----
Uses the same .env file (or env vars) as app.py: GOOGLE_SHEET_ID and either
GOOGLE_CREDENTIALS_FILE or GOOGLE_CREDENTIALS_JSON. Set those up (see
README.md), then just run:

    python init_sheet.py

To also import data:

    python init_sheet.py --tas tas.csv --students students.csv

tas.csv should have columns:      TA_ID, TA_Name, Email, Password
students.csv should have columns: StudentID, StudentName, StudentEmail, TA_Email
"""
import os
import sys
import json
import argparse
import csv

from dotenv import load_dotenv
load_dotenv()  # reads a .env file in this folder, if present, into os.environ

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

TABS = {
    "TAs": ["TA_ID", "TA_Name", "Email", "Password"],
    "Students": ["StudentID", "StudentName", "StudentEmail", "TA_Email"],
}


def get_sheet():
    creds_file = os.environ.get("GOOGLE_CREDENTIALS_FILE")
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        sys.exit("Please set GOOGLE_SHEET_ID (in your .env file or as an env var) first.")
    if creds_file:
        with open(creds_file, "r", encoding="utf-8") as f:
            creds_dict = json.load(f)
    elif creds_json:
        creds_dict = json.loads(creds_json)
    else:
        sys.exit("Please set GOOGLE_CREDENTIALS_FILE (path to the .json key) or "
                  "GOOGLE_CREDENTIALS_JSON (its contents) first.")
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(sheet_id)


def ensure_tabs(sheet):
    existing = {ws.title for ws in sheet.worksheets()}
    for name, headers in TABS.items():
        if name not in existing:
            ws = sheet.add_worksheet(title=name, rows=1000, cols=len(headers))
            print(f"Created tab: {name}")
        else:
            ws = sheet.worksheet(name)
            print(f"Tab already exists: {name}")
        first_row = ws.row_values(1)
        if first_row != headers:
            ws.update("A1", [headers])
            print(f"  -> wrote headers: {headers}")


def import_csv(sheet, tab_name, csv_path):
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = [list(row.values()) for row in reader]
    if not rows:
        print(f"No rows found in {csv_path}, skipping.")
        return
    ws = sheet.worksheet(tab_name)
    ws.append_rows(rows, value_input_option="USER_ENTERED")
    print(f"Imported {len(rows)} rows into {tab_name} from {csv_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tas", help="CSV file with columns TA_ID,TA_Name,Email,Password")
    parser.add_argument("--students", help="CSV file with columns StudentID,StudentName,StudentEmail,TA_Email")
    args = parser.parse_args()

    sheet = get_sheet()
    ensure_tabs(sheet)

    if args.tas:
        import_csv(sheet, "TAs", args.tas)
    if args.students:
        import_csv(sheet, "Students", args.students)

    print("\nDone. Open the sheet to double check everything looks right.")
