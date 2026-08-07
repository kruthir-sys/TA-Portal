import os
import re
import json
from datetime import datetime, date
from functools import wraps

from dotenv import load_dotenv
load_dotenv()  # reads a .env file in this folder, if present, into os.environ

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import gspread
from gspread.exceptions import WorksheetNotFound
from google.oauth2.service_account import Credentials

SESSION_HEADERS = ["StudentID", "StudentName", "Date", "Allocated TA", "Attendance", "Marks", "LastUpdated", "Locked"]

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Simple in-process cache so we don't hit the Sheets API on every click.
_cache = {"client": None, "sheet": None}


def get_client():
    if _cache["client"]:
        return _cache["client"]
    creds_dict = _load_credentials_dict()
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    _cache["client"] = gspread.authorize(creds)
    return _cache["client"]


def _load_credentials_dict():
    """
    Supports two ways of providing the service-account credentials:
    - GOOGLE_CREDENTIALS_FILE: path to the downloaded .json key file (handy locally / in .env)
    - GOOGLE_CREDENTIALS_JSON: the full JSON contents as a string (handy on Render, where
      there's no file to point to - paste the whole file's contents as the env var value)
    """
    creds_file = os.environ.get("GOOGLE_CREDENTIALS_FILE")
    if creds_file:
        with open(creds_file, "r", encoding="utf-8") as f:
            return json.load(f)
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        return json.loads(creds_json)
    raise RuntimeError(
        "Set either GOOGLE_CREDENTIALS_FILE (path to your service-account .json) "
        "or GOOGLE_CREDENTIALS_JSON (its full contents) as an env var."
    )


def get_sheet():
    if _cache["sheet"]:
        return _cache["sheet"]
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise RuntimeError("GOOGLE_SHEET_ID env var not set")
    client = get_client()
    _cache["sheet"] = client.open_by_key(sheet_id)
    return _cache["sheet"]


def get_worksheet(name):
    return get_sheet().worksheet(name)


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("ta_email"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def get_my_students(ta_email):
    ws = get_worksheet("Students")
    records = ws.get_all_records()
    return [r for r in records if str(r.get("TA_Email", "")).strip().lower() == ta_email]


def get_all_students():
    """Full roster from the Students tab, in the exact order it appears there."""
    ws = get_worksheet("Students")
    return ws.get_all_records()


def get_ta_name_map():
    """Maps TA email (lowercased) -> TA_Name, for labelling the roster nicely."""
    try:
        ws = get_worksheet("TAs")
        records = ws.get_all_records()
        return {str(r.get("Email", "")).strip().lower(): r.get("TA_Name", "") for r in records}
    except Exception:
        return {}


ALLOWED_MARKS = {"", "0", "1", "2", "3"}


def is_locked(value):
    return str(value or "").strip().lower() in ("yes", "true", "y", "1")


app.jinja_env.globals["is_locked"] = is_locked


def sanitize_title(name):
    # Google Sheets tab titles can't contain [ ] * ? : / \ and have a length limit.
    name = re.sub(r"[\[\]\*\?:/\\]", "", name).strip()
    name = re.sub(r"\s+", " ", name)
    return (name or "Session")[:100]


def session_sheet_title(date_str):
    return sanitize_title(f"Lab - {date_str}")


def get_or_create_session_ws(date_str):
    sheet = get_sheet()
    title = session_sheet_title(date_str)
    try:
        ws = sheet.worksheet(title)
        if ws.row_values(1) != SESSION_HEADERS:
            ws.update("A1", [SESSION_HEADERS])
        return ws
    except WorksheetNotFound:
        pass

    # First time this date is opened: pre-populate the full roster,
    # in the same order the students appear in the Students tab.
    try:
        all_students = get_all_students()
    except Exception:
        all_students = []
    ta_names = get_ta_name_map()

    roster_rows = []
    for s in all_students:
        sid = str(s.get("StudentID", ""))
        if not sid:
            continue
        ta_email = str(s.get("TA_Email", "")).strip().lower()
        allocated_ta = ta_names.get(ta_email) or s.get("TA_Email", "")
        roster_rows.append([sid, s.get("StudentName", ""), date_str, allocated_ta, "", "", "", ""])

    ws = sheet.add_worksheet(title=title, rows=max(len(roster_rows) + 20, 100), cols=len(SESSION_HEADERS))
    ws.update("A1", [SESSION_HEADERS] + roster_rows)
    return ws


def save_session_rows(ws, new_rows):
    """
    Update new_rows (list of row-lists keyed by StudentID in column 0) into the
    worksheet IN PLACE, preserving the existing row order (which matches the
    Students tab order). Rows for students not in new_rows are left untouched.
    A StudentID not already present gets appended at the end as a fallback.
    """
    existing = ws.get_all_records()
    rows = [[rec.get(h, "") for h in SESSION_HEADERS] for rec in existing]
    id_to_index = {str(r[0]): i for i, r in enumerate(rows) if r and r[0] != ""}

    for nr in new_rows:
        sid = str(nr[0])
        if sid in id_to_index:
            rows[id_to_index[sid]] = nr
        else:
            rows.append(nr)
            id_to_index[sid] = len(rows) - 1

    ws.clear()
    ws.update("A1", [SESSION_HEADERS] + rows)


# ---------------- Auth ----------------

@app.route("/")
def index():
    if session.get("ta_email"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        try:
            ws = get_worksheet("TAs")
            records = ws.get_all_records()
        except Exception as e:
            flash(f"Could not reach Google Sheet: {e}", "error")
            return redirect(url_for("login"))

        for row in records:
            row_email = str(row.get("Email", "")).strip().lower()
            row_password = str(row.get("Password", "")).strip()
            if row_email == email and row_password == password and email:
                session["ta_email"] = row_email
                session["ta_name"] = row.get("TA_Name", email)
                session["ta_id"] = row.get("TA_ID", "")
                return redirect(url_for("dashboard"))

        flash("Invalid email or password.", "error")
        return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------- Dashboard ----------------

@app.route("/dashboard")
@login_required
def dashboard():
    try:
        students = get_my_students(session["ta_email"])
    except Exception as e:
        flash(f"Could not load students: {e}", "error")
        students = []
    return render_template("dashboard.html", students=students, ta_name=session.get("ta_name"))


# ---------------- Attendance & Marks ----------------

@app.route("/session", methods=["GET"])
@login_required
def session_form():
    ta_email = session["ta_email"]
    try:
        students = get_my_students(ta_email)
    except Exception as e:
        flash(f"Could not load data: {e}", "error")
        students = []

    sel_date = date.today().isoformat()  # always today - no picker, TAs mark same-day attendance only

    existing = {}
    if students:
        try:
            ws = get_or_create_session_ws(sel_date)
            for rec in ws.get_all_records():
                sid = str(rec.get("StudentID", ""))
                if sid:
                    existing[sid] = rec
        except Exception as e:
            flash(f"Could not load existing session data: {e}", "error")

    return render_template(
        "session.html",
        students=students, sel_date=sel_date,
        existing=existing, ta_name=session.get("ta_name"),
        student_info={
            str(s.get("StudentID")): {
                "name": s.get("StudentName", ""),
                "seatNo": s.get("Seat No", ""),
            }
            for s in students
        },
    )


@app.route("/session/mark", methods=["POST"])
@login_required
def session_mark():
    """
    Quick-mark endpoint used by the "type a Student ID" box on the session
    page. Marks/locks exactly one student and returns JSON so the page can
    update instantly without a full reload - built for fast phone use.
    """
    ta_email = session["ta_email"]
    ta_name = session.get("ta_name") or ta_email
    sel_date = date.today().isoformat()  # always today, regardless of what was submitted
    sid = request.form.get("student_id", "").strip()
    att = request.form.get("attendance", "P").strip() or "P"
    marks_val = request.form.get("marks", "").strip()

    if not sid:
        return jsonify(ok=False, error="Enter a Student ID."), 400
    if marks_val not in ALLOWED_MARKS:
        return jsonify(ok=False, error="Marks must be 0, 1, 2, or 3."), 400

    try:
        students = get_my_students(ta_email)
    except Exception as e:
        return jsonify(ok=False, error=f"Could not load your students: {e}"), 500

    match = next((s for s in students if str(s.get("StudentID")) == sid), None)
    if not match:
        return jsonify(ok=False, error=f"Student ID {sid} is not assigned to you."), 404

    try:
        ws = get_or_create_session_ws(sel_date)
        existing_records = ws.get_all_records()
    except Exception as e:
        return jsonify(ok=False, error=f"Could not open the sheet: {e}"), 500

    existing_row = next((r for r in existing_records if str(r.get("StudentID")) == sid), None)
    if existing_row and is_locked(existing_row.get("Locked")):
        return jsonify(
            ok=False,
            error=f"{match.get('StudentName')} is already marked and locked.",
            name=match.get("StudentName"),
            attendance=existing_row.get("Attendance"),
            marks=existing_row.get("Marks"),
        ), 409

    now_str = datetime.now().isoformat(timespec="seconds")
    new_row = [sid, match.get("StudentName"), sel_date, ta_name, att, marks_val, now_str, "Yes"]

    try:
        save_session_rows(ws, [new_row])
    except Exception as e:
        return jsonify(ok=False, error=f"Could not save: {e}"), 500

    return jsonify(ok=True, name=match.get("StudentName"), attendance=att, marks=marks_val)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
