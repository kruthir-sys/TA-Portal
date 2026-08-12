from flask import Flask, render_template, request, redirect, session, flash
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import os
import json
import threading
import time
import re

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
TA_PASSWORD = os.environ.get("TA_PASSWORD", "ta123")
INSTRUCTOR_PASSWORD = os.environ.get("INSTRUCTOR_PASSWORD", "instr123")

# ================= GOOGLE SHEETS SETUP =================
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

# ================= GOOGLE CREDENTIALS =================
# On Render, the complete service-account JSON is stored in
# GOOGLE_CREDENTIALS_JSON.
google_credentials_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")

if not google_credentials_json:
    raise RuntimeError(
        "GOOGLE_CREDENTIALS_JSON environment variable is missing. "
        "Add the complete Google service-account JSON in Render."
    )

try:
    credentials_info = json.loads(google_credentials_json)
except json.JSONDecodeError as exc:
    raise RuntimeError(
        "GOOGLE_CREDENTIALS_JSON is not valid JSON."
    ) from exc

creds = ServiceAccountCredentials.from_json_keyfile_dict(
    credentials_info,
    scope
)

client = gspread.authorize(creds)

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")

if not GOOGLE_SHEET_ID:
    raise RuntimeError(
        "GOOGLE_SHEET_ID environment variable is missing. "
        "Add it in Render."
    )

# One process + threads is recommended for this Google Sheets-backed app.
# It lets the in-process lock protect simultaneous grading submissions.
write_lock = threading.Lock()

# Short-lived caches reduce repeated full-sheet reads while keeping marks
# responsive. Marks cache is invalidated immediately after writes.
_cache_lock = threading.Lock()
_students_cache = {"data": None, "expires": 0}
_tas_cache = {"data": None, "expires": 0}
_instructors_cache = {"data": None, "expires": 0}
_marks_cache = {"data": None, "expires": 0}

STUDENTS_CACHE_SECONDS = 10
TA_CACHE_SECONDS = 30
INSTRUCTOR_CACHE_SECONDS = 30
MARKS_CACHE_SECONDS = 3


def cached_records(ws, cache, ttl):
    now = time.time()

    with _cache_lock:
        if cache["data"] is not None and now < cache["expires"]:
            return cache["data"]

    data = ws.get_all_records()

    with _cache_lock:
        cache["data"] = data
        cache["expires"] = now + ttl

    return data


def invalidate_marks_cache():
    with _cache_lock:
        _marks_cache["data"] = None
        _marks_cache["expires"] = 0


def get_students():
    return cached_records(
        students_ws,
        _students_cache,
        STUDENTS_CACHE_SECONDS
    )


def get_tas():
    return cached_records(
        ta_ws,
        _tas_cache,
        TA_CACHE_SECONDS
    )


def get_instructors():
    return cached_records(
        instructor_ws,
        _instructors_cache,
        INSTRUCTOR_CACHE_SECONDS
    )


def get_marks():
    return cached_records(
        marks_ws,
        _marks_cache,
        MARKS_CACHE_SECONDS
    )


def normalize_student_id(value):
    """
    Normalize a student ID for searching.

    Examples:
        2026A7PS0702G -> 2026A7PS0702
        2026A7PS0702  -> 2026A7PS0702
        2026 a7ps 0702 g -> 2026A7PS0702
        0702 -> 0702
    """
    value = str(value or "").strip().upper()

    # Remove spaces and common separators that may be present in Sheets.
    value = re.sub(r"[^A-Z0-9]", "", value)

    # Ignore ONLY the final G.
    if value.endswith("G"):
        value = value[:-1]

    return value


def record_value(record, *possible_names):
    """
    Read a field even if the Google Sheet header has spaces,
    underscores, different capitalization, etc.
    """
    normalized_headers = {}

    for key, value in record.items():
        normalized_key = re.sub(
            r"[^A-Z0-9]",
            "",
            str(key).strip().upper()
        )
        normalized_headers[normalized_key] = value

    for name in possible_names:
        normalized_name = re.sub(
            r"[^A-Z0-9]",
            "",
            str(name).strip().upper()
        )

        if normalized_name in normalized_headers:
            return normalized_headers[normalized_name]

    return ""


def student_id_from_record(student):
    return record_value(
        student,
        "Student_ID",
        "Student ID",
        "StudentID",
        "BITS ID",
        "BITS_ID",
        "ID"
    )


def student_name_from_record(student):
    return record_value(
        student,
        "Student_Name",
        "Student Name",
        "StudentName",
        "Name"
    )


def status_to_bit(is_present):
    """Frontend still works with Present/Absent; sheet stores 1/0."""
    return 1 if is_present else 0


def bit_to_status(raw_value):
    """Convert whatever is stored in the sheet (1/0, '1'/'0', or legacy
    'Present'/'Absent' text) back into the Present/Absent strings the
    templates already expect, so the frontend needs no changes."""
    value = str(raw_value).strip().lower()
    if value in ("1", "1.0", "present", "true", "yes"):
        return "Present"
    return "Absent"


def find_student(students, search_value):
    search_value = normalize_student_id(search_value)

    if not search_value:
        return None

    for student in students:
        sid = normalize_student_id(student_id_from_record(student))

        if sid == search_value:
            return student

        if len(search_value) == 4 and sid.endswith(search_value):
            return student

    return None


sheet = client.open_by_key(GOOGLE_SHEET_ID)

students_ws = sheet.worksheet("Students")
marks_ws = sheet.worksheet("Marks")
ta_ws = sheet.worksheet("TA_List")
instructor_ws = sheet.worksheet("Instructors")
ta_attendance_ws = sheet.worksheet("TA_Attendance")


@app.route("/health")
def health():
    return "OK", 200

# ================= LOGIN =================
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")

        instructors = get_instructors()
        tas = get_tas()

        # Instructor login
        for ins in instructors:
            full_name = str(ins.get("Instructor_Name", "")).strip()
            
            if not full_name:
                continue

            first_name = full_name.split()[0].lower()

            if username == first_name and password == INSTRUCTOR_PASSWORD:
                # Store the actual full name
                session["user"] = full_name
                session["role"] = "instructor"

                return redirect("/dashboard")

        # TA login
        for ta in tas:
            full_name = str(ta.get("TA_Name", "")).strip()

            if not full_name:
                continue

            first_name = full_name.split()[0].lower()

            if username == first_name and password == TA_PASSWORD:
                # Store the actual full name
                session["user"] = full_name
                session["role"] = "ta"

                return redirect("/dashboard")

        flash("Invalid login", "error")

    return render_template("login.html")
# ================= DASHBOARD =================
@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if "user" not in session:
        return redirect("/")

    role = session["role"]
    today = datetime.now().strftime("%Y-%m-%d")
    search_query = request.args.get("search", "").strip()

    # Always read Students directly for searching.
    students = students_ws.get_all_records()
    marks = marks_ws.get_all_records()
    tas = get_tas()

    search_results = []

    if search_query:
        q = normalize_student_id(search_query)

        for student in students:
            # EXACT Students-sheet headers supplied by the user.
            raw_id = str(student.get("Student ID", "")).strip()
            name = str(student.get("Student Name", "")).strip()

            sid = normalize_student_id(raw_id)

            if not sid:
                continue

            # Match full ID OR last four digits.
            if sid == q or (len(q) == 4 and sid.endswith(q)):
                view = {
                    "Student_ID": raw_id,
                    "Student_Name": name,
                    "graded": False,
                    "existing_marks": "",
                    "graded_by": "",
                    "graded_date": ""
                }

                # EXACT Marks-sheet headers supplied by the user.
                # Only today's grade is considered the current grade.
                for mark in marks:
                    mark_id = normalize_student_id(
                        str(mark.get("Student_ID", "")).strip()
                    )
                    mark_date = str(
                        mark.get("Date", "")
                    ).strip()

                    if mark_id == sid and mark_date == today:
                        view["graded"] = True
                        view["existing_marks"] = mark.get("Marks", "")
                        view["graded_by"] = mark.get("TA", "")
                        view["graded_date"] = mark_date
                        break

                search_results.append(view)

    # ---------- SAVE / UPDATE MARKS ----------
    if request.method == "POST":
        submitted_id = str(
            request.form.get("student_id", "")
        ).strip()
        submitted_marks = str(
            request.form.get("marks", "")
        ).strip()

        sid = normalize_student_id(submitted_id)

        if not sid or not submitted_marks:
            flash("Please enter the student ID and marks.", "error")
            return redirect(url_for_dashboard(search_query))

        matched_student = None

        for student in students:
            raw_id = str(student.get("Student ID", "")).strip()

            if normalize_student_id(raw_id) == sid:
                matched_student = student
                break

        if matched_student is None:
            flash("Student not found.", "error")
            return redirect(url_for_dashboard(
                sid[-4:] if len(sid) >= 4 else sid
            ))

        try:
            marks_value = float(submitted_marks)
            if marks_value.is_integer():
                marks_value = int(marks_value)
        except ValueError:
            flash("Marks must be a number.", "error")
            return redirect(url_for_dashboard(
                sid[-4:] if len(sid) >= 4 else sid
            ))

        with write_lock:
            latest_marks = marks_ws.get_all_records()

            today_row = None
            today_record = None

            for row_no, mark in enumerate(latest_marks, start=2):
                mark_id = normalize_student_id(
                    str(mark.get("Student_ID", "")).strip()
                )
                mark_date = str(mark.get("Date", "")).strip()

                if mark_id == sid and mark_date == today:
                    today_row = row_no
                    today_record = mark
                    break

            # TA cannot change a grade already entered today.
            if role == "ta" and today_record is not None:
                invalidate_marks_cache()
                flash(
                    "This student is already marked today. "
                    "TA marks cannot be changed.",
                    "warning"
                )
                return redirect(url_for_dashboard(
                    sid[-4:] if len(sid) >= 4 else sid
                ))

            if today_row is not None:
                # Instructor updates today's grade.
                marks_ws.update(
                    f"C{today_row}:E{today_row}",
                    [[marks_value, session["user"], today]],
                    value_input_option="USER_ENTERED"
                )
                message = "Marks updated successfully."
            else:
                # New record for today's date.
                marks_ws.append_row(
                    [
                        matched_student.get("Student ID", ""),
                        matched_student.get("Student Name", ""),
                        marks_value,
                        session["user"],
                        today
                    ],
                    value_input_option="USER_ENTERED"
                )
                message = "Marks submitted successfully."

            invalidate_marks_cache()

        flash(message, "success")
        return redirect(url_for_dashboard(
            sid[-4:] if len(sid) >= 4 else sid
        ))

    # ---------- TODAY'S STATISTICS ----------
    marks_today = [
        m for m in marks
        if str(m.get("Date", "")).strip() == today
    ]

    total_students = len(students)
    graded = len(marks_today)

    ta_names = [
        str(t.get("TA_Name", "")).strip()
        for t in tas
        if str(t.get("TA_Name", "")).strip()
    ]

    ta_counts = {name: 0 for name in ta_names}

    for mark in marks_today:
        grader = str(mark.get("TA", "")).strip()
        if grader in ta_counts:
            ta_counts[grader] += 1

    graded_by_ta = sum(ta_counts.values())

    graded_by_instructor = sum(
        1
        for mark in marks_today
        if str(mark.get("TA", "")).strip() not in ta_counts
    )

    # ---------- TODAY'S TA ATTENDANCE ----------
    ta_attendance = {}

    if role == "instructor":
        attendance_records = ta_attendance_ws.get_all_records()

        ta_attendance = {
            name: "Absent"
            for name in ta_names
        }

        for record in attendance_records:
            ta_name = str(
                record.get("TA_Name", "")
            ).strip()
            date = str(
                record.get("Date", "")
            ).strip()

            if date == today and ta_name in ta_attendance:
                ta_attendance[ta_name] = bit_to_status(
                    record.get("Status", "")
                )

    return render_template(
        "dashboard.html",
        role=role,
        search_results=search_results,
        search_query=search_query,
        total_students=total_students,
        graded=graded,
        graded_by_ta=graded_by_ta,
        graded_by_instructor=graded_by_instructor,
        ta_counts=ta_counts,
        ta_attendance=ta_attendance,
        today=today
    )


def url_for_dashboard(search_value=""):
    if search_value:
        from urllib.parse import quote
        return "/dashboard?search=" + quote(str(search_value))
    return "/dashboard"


# ================= ATTENDANCE =================
@app.route("/attendance", methods=["GET", "POST"])
def attendance():

    if "role" not in session or session["role"] != "instructor":
        return redirect("/dashboard")

    tas = get_tas()
    ta_names = [
        str(ta.get("TA_Name", "")).strip()
        for ta in tas
        if str(ta.get("TA_Name", "")).strip()
    ]

    today = datetime.now().strftime("%Y-%m-%d")

    def load_today_attendance():
        records = ta_attendance_ws.get_all_records()

        result = {name: "Absent" for name in ta_names}

        for record in records:
            name = str(record.get("TA_Name", "")).strip()
            date = str(record.get("Date", "")).strip()
            status = record.get("Status", "")

            if date == today and name in result:
                result[name] = bit_to_status(status)

        return result

    if request.method == "POST":

        selected = set(request.form.getlist("ta_names"))

        with write_lock:

            records = ta_attendance_ws.get_all_records()
            existing_rows = {}

            for row_number, record in enumerate(records, start=2):
                name = str(record.get("TA_Name", "")).strip()
                date = str(record.get("Date", "")).strip()

                if date == today and name in ta_names:
                    existing_rows[name] = row_number

            for name in ta_names:

                is_present = name in selected
                status_bit = status_to_bit(is_present)

                if name in existing_rows:

                    row = existing_rows[name]

                    ta_attendance_ws.update(
                        f"A{row}:C{row}",
                        [[name, today, status_bit]],
                        value_input_option="USER_ENTERED"
                    )

                else:

                    ta_attendance_ws.append_row(
                        [name, today, status_bit],
                        value_input_option="USER_ENTERED"
                    )

        flash("TA attendance saved successfully.", "success")
        return redirect("/dashboard")

    return render_template(
        "attendance.html",
        tas=tas,
        today=today,
        ta_attendance=load_today_attendance()
    )


# ================= LOGOUT =================
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

if __name__ == "__main__":
    # Local development only. Render uses Gunicorn.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
