from flask import Flask, render_template, request, redirect, session, flash
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import os
import json
import threading
import time

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

STUDENTS_CACHE_SECONDS = 300
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
    value = str(value or "").strip().upper()
    # Ignore only the final G in IDs such as 2026A7PS0702G.
    return value[:-1] if value.endswith("G") else value


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

    for student in students:
        sid = normalize_student_id(student.get("Student_ID", ""))

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
        username = request.form["username"]
        password = request.form["password"]

        instructors = get_instructors()
        tas = get_tas()

        # Instructor login
        for ins in instructors:
            if ins["Instructor_Name"] == username and password == INSTRUCTOR_PASSWORD:
                session["user"] = username
                session["role"] = "instructor"
                return redirect("/dashboard")

        # TA login
        for ta in tas:
            if ta["TA_Name"] == username and password == TA_PASSWORD:
                session["user"] = username
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

    students = get_students()
    marks = get_marks()
    tas = get_tas()

    today = datetime.now().strftime("%Y-%m-%d")

    # Normalize IDs once.  The final G is ignored.
    normalized_students = {}
    for student in students:
        sid = normalize_student_id(student.get("Student_ID", ""))
        if sid:
            normalized_students[sid] = student

    # ===== SEARCH =====
    search_query = request.args.get("search", "").strip()
    search_value = normalize_student_id(search_query)
    search_results = []

    if search_value:
        for sid, student in normalized_students.items():

            # Full ID OR last 4 digits.
            if sid != search_value and not (
                len(search_value) == 4 and sid.endswith(search_value)
            ):
                continue

            student_view = dict(student)

            # IMPORTANT:
            # Grading is date-wise. Only today's Marks record locks the
            # student for today's grading and is displayed as the current grade.
            existing = next(
                (
                    m for m in marks
                    if normalize_student_id(m.get("Student_ID", "")) == sid
                    and str(m.get("Date", "")).strip() == today
                ),
                None
            )

            student_view["graded"] = existing is not None
            student_view["existing_marks"] = (
                existing.get("Marks", "") if existing else ""
            )
            student_view["graded_by"] = (
                existing.get("TA", "") if existing else ""
            )
            student_view["graded_date"] = (
                existing.get("Date", "") if existing else ""
            )

            search_results.append(student_view)

    # ===== SUBMIT / UPDATE MARKS =====
    if request.method == "POST":

        student_id = str(
            request.form.get("student_id", "")
        ).strip()

        new_marks = str(
            request.form.get("marks", "")
        ).strip()

        if not student_id or new_marks == "":
            flash("Please enter a student ID and marks.", "error")
            return redirect(url_for_dashboard(search_query))

        normalized_student_id = normalize_student_id(student_id)

        matched = next(
            (
                s for s in students
                if normalize_student_id(s.get("Student_ID", "")) == normalized_student_id
            ),
            None
        )

        if not matched:
            flash("Student not found.", "error")
            return redirect("/dashboard")

        # Convert marks to a real number.
        try:
            marks_number = float(new_marks)
            if marks_number.is_integer():
                marks_number = int(marks_number)
        except (ValueError, TypeError):
            flash("Marks must be a number.", "error")
            return redirect(
                url_for_dashboard(normalized_student_id[-4:])
            )

        with write_lock:

            # Always re-read the sheet before checking today's grade.
            latest_marks = marks_ws.get_all_records()

            existing_today = next(
                (
                    m for m in latest_marks
                    if normalize_student_id(m.get("Student_ID", "")) == normalized_student_id
                    and str(m.get("Date", "")).strip() == today
                ),
                None
            )

            # TA cannot modify today's existing grade.
            if role == "ta" and existing_today:
                invalidate_marks_cache()
                flash(
                    "This student is already graded today. TA marks are locked.",
                    "warning"
                )
                return redirect(
                    url_for_dashboard(normalized_student_id[-4:])
                )

            if existing_today:
                # Instructor can change today's grade.
                # Locate the row using the normalized ID AND today's date.
                row = None

                for row_number, record in enumerate(latest_marks, start=2):
                    if (
                        normalize_student_id(record.get("Student_ID", "")) == normalized_student_id
                        and str(record.get("Date", "")).strip() == today
                    ):
                        row = row_number
                        break

                if row is None:
                    flash("Today's marks record could not be found.", "error")
                    return redirect(
                        url_for_dashboard(normalized_student_id[-4:])
                    )

                marks_ws.update(
                    f"C{row}:E{row}",
                    [[marks_number, session["user"], today]],
                    value_input_option="USER_ENTERED"
                )

                message = "Marks updated successfully."

            else:
                # No grade exists for TODAY.
                # Therefore a new record is appended even if this student
                # was graded on a previous date.
                marks_ws.append_row(
                    [
                        matched["Student_ID"],
                        matched["Student_Name"],
                        marks_number,
                        session["user"],
                        today
                    ],
                    value_input_option="USER_ENTERED"
                )

                message = "Marks submitted successfully."

            invalidate_marks_cache()

        flash(message, "success")

        return redirect(
            url_for_dashboard(normalized_student_id[-4:])
        )

    # ===== STATS — TODAY ONLY =====
    marks_today = [
        mark for mark in marks
        if str(mark.get("Date", "")).strip() == today
    ]

    total_students = len(students)
    graded = len(marks_today)

    ta_names = [
        str(t.get("TA_Name", "")).strip()
        for t in tas
        if str(t.get("TA_Name", "")).strip()
    ]

    ta_counts = {ta: 0 for ta in ta_names}

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

    # Today's TA attendance for instructor dashboard.
    ta_attendance = {}

    if role == "instructor":
        attendance_records = ta_attendance_ws.get_all_records()

        ta_attendance = {
            ta: "Absent"
            for ta in ta_names
        }

        for record in attendance_records:
            ta_name = str(record.get("TA_Name", "")).strip()
            date = str(record.get("Date", "")).strip()
            status = record.get("Status", "")

            if date == today and ta_name in ta_attendance:
                ta_attendance[ta_name] = bit_to_status(status)

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
