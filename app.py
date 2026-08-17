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

# ================= DEPLOYMENT REQUIREMENT =================
# This app protects concurrent grading/attendance writes using in-process
# locks (threading.Lock) and in-process caches (plain dicts). Both ONLY
# work correctly if the whole app runs as a SINGLE process — multiple
# threads inside that one process are fine and expected (that's how 15+
# TAs and 4 instructors can use it at once), but multiple worker
# PROCESSES are not: each process would get its own separate lock and
# cache, so two people hitting different processes could race on the
# same Google Sheet row and silently overwrite each other's grade.
#
# On Render (or any Gunicorn-based host), the start command must use
# exactly one worker with several threads, e.g.:
#     gunicorn app:app --workers 1 --threads 8 --timeout 60
# Do NOT increase --workers above 1 unless the locking/caching below is
# moved to something shared across processes (e.g. Redis).

# Two separate locks so grading and attendance don't block each other —
# they touch different worksheets and have nothing to do with one another.
marks_lock = threading.Lock()
attendance_lock = threading.Lock()

# Short-lived caches reduce repeated full-sheet reads while keeping marks
# responsive. Marks cache is invalidated immediately after writes.
_cache_lock = threading.Lock()
_students_cache = {"data": None, "expires": 0}
_tas_cache = {"data": None, "expires": 0}
_instructors_cache = {"data": None, "expires": 0}
_marks_cache = {"data": None, "expires": 0}

# The student roster rarely changes during a grading session, so it can
# be cached longer. Marks and attendance change constantly, so they stay
# short — long enough to absorb bursts of concurrent page loads, short
# enough that nobody sees stale data for more than a few seconds. All
# are env-overridable in case load patterns change.
STUDENTS_CACHE_SECONDS = int(os.environ.get("STUDENTS_CACHE_SECONDS", 60))
TA_CACHE_SECONDS = int(os.environ.get("TA_CACHE_SECONDS", 30))
INSTRUCTOR_CACHE_SECONDS = int(os.environ.get("INSTRUCTOR_CACHE_SECONDS", 60))
MARKS_CACHE_SECONDS = int(os.environ.get("MARKS_CACHE_SECONDS", 3))

# ================= RESILIENT SHEETS ACCESS =================
# Google Sheets API returns HTTP 429 (rate limited) or transient 5xx
# errors under bursty concurrent load. Without retries, a TA's grade
# submission could fail outright even though nothing was actually wrong
# with the data — that's the #1 way this kind of app "loses" a grade
# from the user's point of view. Every read/write goes through here so
# a transient hiccup is retried instead of surfacing as a lost update.
SHEETS_MAX_ATTEMPTS = int(os.environ.get("SHEETS_MAX_ATTEMPTS", 4))
SHEETS_RETRY_BASE_DELAY = float(os.environ.get("SHEETS_RETRY_BASE_DELAY", 0.6))


def with_retry(func, *args, **kwargs):
    attempt = 0

    while True:
        attempt += 1

        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as exc:
            status = None
            try:
                status = exc.response.status_code
            except Exception:
                pass

            # Only retry on rate limiting / transient server errors.
            # A genuine permission or bad-request error should surface
            # immediately rather than retrying uselessly.
            retryable = status is None or status == 429 or status >= 500

            if not retryable or attempt >= SHEETS_MAX_ATTEMPTS:
                raise

            time.sleep(SHEETS_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
        except Exception:
            if attempt >= SHEETS_MAX_ATTEMPTS:
                raise
            time.sleep(SHEETS_RETRY_BASE_DELAY * (2 ** (attempt - 1)))


def safe_get_all_records(ws):
    return with_retry(ws.get_all_records)


def safe_update(ws, range_name, values, **kwargs):
    return with_retry(ws.update, range_name, values, **kwargs)


def safe_append_row(ws, row, **kwargs):
    return with_retry(ws.append_row, row, **kwargs)


def safe_batch_update(ws, data, **kwargs):
    return with_retry(ws.batch_update, data, **kwargs)


def safe_append_rows(ws, rows, **kwargs):
    return with_retry(ws.append_rows, rows, **kwargs)


def cached_records(ws, cache, ttl):
    now = time.time()

    with _cache_lock:
        if cache["data"] is not None and now < cache["expires"]:
            return cache["data"]

    data = safe_get_all_records(ws)

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


def ta_present_today(ta_name, today):
    """True if this TA was marked Present in at least one session
    (1, 2, or 3) today. Instructors are never gated by this check."""
    ta_name = str(ta_name).strip()
    if not ta_name:
        return False

    records = safe_get_all_records(ta_attendance_ws)

    for record in records:
        record_name = str(record.get("TA_Name", "")).strip()
        record_date = str(record.get("Date", "")).strip()

        if (
            record_name == ta_name
            and record_date == today
            and bit_to_status(record.get("Status", "")) == "Present"
        ):
            return True

    return False


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

    # Cached reads instead of hitting the Sheets API on every single page
    # load — at 15 TAs + 4 instructors this matters a lot. The marks
    # cache is invalidated the instant anyone writes, so nobody sees
    # data more than a few seconds stale.
    students = get_students()
    marks = get_marks()
    tas = get_tas()

    # Build today's grading lookup ONCE per request instead of scanning
    # the entire marks list for every matched student. With up to ~1000
    # students and a growing Marks sheet, this turns what used to be an
    # O(students x marks) nested scan into a single O(marks) pass.
    marks_today_by_id = {}
    for mark in marks:
        if str(mark.get("Date", "")).strip() != today:
            continue
        mark_id = normalize_student_id(
            str(mark.get("Student_ID", "")).strip()
        )
        if mark_id:
            marks_today_by_id[mark_id] = mark

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

                # Only today's grade is considered the current grade.
                mark = marks_today_by_id.get(sid)

                if mark is not None:
                    view["graded"] = True
                    view["existing_marks"] = mark.get("Marks", "")
                    view["graded_by"] = mark.get("TA", "")
                    view["graded_date"] = str(mark.get("Date", "")).strip()

                search_results.append(view)

    # ---------- SAVE / UPDATE MARKS ----------
    if request.method == "POST":

        # A TA must be marked present (any session) today before they
        # can grade students. Instructors are never gated by this.
        if role == "ta" and not ta_present_today(session["user"], today):
            flash(
                "You must be marked present for today's session before "
                "you can grade students. Ask your instructor to mark "
                "your attendance.",
                "error"
            )
            return redirect(url_for_dashboard(search_query))

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

        ALLOWED_MARKS = (0, 2, 5)
        if marks_value not in ALLOWED_MARKS:
            flash(
                "Marks must be one of: " +
                ", ".join(str(m) for m in ALLOWED_MARKS) + ".",
                "error"
            )
            return redirect(url_for_dashboard(
                sid[-4:] if len(sid) >= 4 else sid
            ))

        with marks_lock:
            latest_marks = safe_get_all_records(marks_ws)

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

            try:
                if today_row is not None:
                    # Instructor updates today's grade.
                    safe_update(
                        marks_ws,
                        f"C{today_row}:E{today_row}",
                        [[marks_value, session["user"], today]],
                        value_input_option="USER_ENTERED"
                    )
                    message = "Marks updated successfully."
                else:
                    # New record for today's date.
                    safe_append_row(
                        marks_ws,
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
            except Exception:
                # The write genuinely failed even after retries — do NOT
                # report success. The cache is invalidated so the next
                # load re-reads the real sheet state instead of trusting
                # anything we assumed here.
                invalidate_marks_cache()
                flash(
                    "Could not save marks right now (connection issue "
                    "with Google Sheets). Please try again in a moment.",
                    "error"
                )
                return redirect(url_for_dashboard(
                    sid[-4:] if len(sid) >= 4 else sid
                ))

            invalidate_marks_cache()

        flash(message, "success")

        # Clear the search after a successful save so the previously
        # searched student's ID/details don't linger on the page.
        return redirect("/dashboard")

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

    # ---------- TODAY'S TA ATTENDANCE (grouped by session) ----------
    # session_attendance: {"1": [names present], "2": [...], "3": [...]}
    session_attendance = {"1": [], "2": [], "3": []}

    if role == "instructor":
        attendance_records = safe_get_all_records(ta_attendance_ws)

        # Track status per (name, session) first so we render TAs in a
        # stable order (ta_names order) rather than sheet row order.
        per_ta_session_status = {
            name: {"1": "Absent", "2": "Absent", "3": "Absent"}
            for name in ta_names
        }

        for record in attendance_records:
            ta_name = str(
                record.get("TA_Name", "")
            ).strip()
            date = str(
                record.get("Date", "")
            ).strip()
            session_no = str(
                record.get("Session", "1")
            ).strip() or "1"

            if (
                date == today
                and ta_name in per_ta_session_status
                and session_no in ("1", "2", "3")
            ):
                per_ta_session_status[ta_name][session_no] = bit_to_status(
                    record.get("Status", "")
                )

        for name in ta_names:
            for session_no in ("1", "2", "3"):
                if per_ta_session_status[name][session_no] == "Present":
                    session_attendance[session_no].append(name)

    # Whether the logged-in TA is cleared to grade today (instructors
    # are always cleared). Computed once per page load and reused for
    # every search result row in the template.
    ta_can_grade = (
        role == "instructor"
        or ta_present_today(session["user"], today)
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
        session_attendance=session_attendance,
        ta_can_grade=ta_can_grade,
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

    VALID_SESSIONS = ("1", "2", "3")

    def current_session():
        # Works for both GET (query string) and POST (form field).
        raw = request.values.get("session", "1").strip()
        return raw if raw in VALID_SESSIONS else "1"

    def load_today_attendance(session_no):
        records = safe_get_all_records(ta_attendance_ws)

        result = {name: "Absent" for name in ta_names}

        for record in records:
            name = str(record.get("TA_Name", "")).strip()
            date = str(record.get("Date", "")).strip()
            rec_session = str(record.get("Session", "1")).strip() or "1"
            status = record.get("Status", "")

            if date == today and rec_session == session_no and name in result:
                result[name] = bit_to_status(status)

        return result

    if request.method == "POST":

        session_no = current_session()
        selected = set(request.form.getlist("ta_names"))

        with attendance_lock:

            records = safe_get_all_records(ta_attendance_ws)
            existing_rows = {}

            for row_number, record in enumerate(records, start=2):
                name = str(record.get("TA_Name", "")).strip()
                date = str(record.get("Date", "")).strip()
                rec_session = str(
                    record.get("Session", "1")
                ).strip() or "1"

                if (
                    date == today
                    and rec_session == session_no
                    and name in ta_names
                ):
                    existing_rows[name] = row_number

            # Batch every TA's row into as few Sheets API calls as
            # possible instead of one call per TA (which, with 15 TAs,
            # meant up to 15 round-trips per submission before this).
            batch_updates = []
            new_rows = []

            for name in ta_names:

                is_present = name in selected
                status_bit = status_to_bit(is_present)

                if name in existing_rows:
                    row = existing_rows[name]
                    batch_updates.append({
                        "range": f"A{row}:D{row}",
                        "values": [[name, today, session_no, status_bit]]
                    })
                else:
                    new_rows.append([name, today, session_no, status_bit])

            try:
                if batch_updates:
                    safe_batch_update(
                        ta_attendance_ws,
                        batch_updates,
                        value_input_option="USER_ENTERED"
                    )

                if new_rows:
                    safe_append_rows(
                        ta_attendance_ws,
                        new_rows,
                        value_input_option="USER_ENTERED"
                    )
            except Exception:
                flash(
                    "Could not save attendance right now (connection "
                    "issue with Google Sheets). Please try again.",
                    "error"
                )
                return redirect(f"/attendance?session={session_no}")

        flash(f"Session {session_no} attendance saved successfully.", "success")
        return redirect(f"/attendance?session={session_no}")

    session_no = current_session()

    return render_template(
        "attendance.html",
        tas=tas,
        today=today,
        session_no=session_no,
        ta_attendance=load_today_attendance(session_no)
    )


# ================= LOGOUT =================
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

if __name__ == "__main__":
    # Local development only. Render uses Gunicorn.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
