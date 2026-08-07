# TA Portal — Marks & Attendance

A small Flask app so each TA can log in on their phone with their email,
see their assigned students, and record attendance + marks for a lab
session. Everything is read from and written to a Google Sheet you
control — no separate database to manage.

**Key idea:** every **date** gets its own worksheet tab, created
automatically the first time any TA opens/saves attendance for that day
(e.g. `Lab - 2026-08-04`). The portal always uses **today's date** — there's
no date picker, since TAs only ever mark same-day attendance/marks. Since
one day usually spans several TAs' groups of students, multiple TAs can
save into the same date's tab —
each TA's save only touches their own students' rows, so nobody
overwrites anyone else's data.

## 1. Set up the Google Sheet

1. Create a new Google Sheet (or use an existing one). Note the **Sheet ID**
   from its URL:
   `https://docs.google.com/spreadsheets/d/`**`THIS_PART_IS_THE_ID`**`/edit`

2. It needs 2 base tabs, with these exact headers in row 1:

   **TAs**
   | TA_ID | TA_Name | Email | Password |
   |---|---|---|---|

   **Students**
   | StudentID | StudentName | StudentEmail | TA_Email |
   |---|---|---|---|

   `TA_Email` in the Students tab must exactly match a row's `Email` in the
   `TAs` tab — this is how the app knows which 30 students belong to which
   TA. (`TA_ID` and `StudentEmail` are stored for your reference / other
   systems; the app itself matches on email.)

   Tip: instead of typing headers by hand, use `init_sheet.py` (see below) —
   it creates both tabs and headers for you, and can also bulk-import your
   existing TA list / student list from CSV files.

3. **Don't create session tabs yourself.** The app creates one
   automatically the first time any TA opens a date, named `"Lab - <Date>"`
   (e.g. `Lab - 2026-08-04`), and immediately pre-fills it with **every
   student from the Students tab, in the exact same order they appear
   there** — not just one TA's group. Columns:

   | StudentID | StudentName | Date | Allocated TA | Attendance | Marks | LastUpdated | Locked |
   |---|---|---|---|---|---|---|---|

   `Attendance` is `P` (present) or `A` (absent) — blank until a TA saves
   it (the form defaults new entries to `P`). **Once a TA saves a
   student's row, it's locked** (`Locked` = `Yes`) — that student becomes
   read-only on the form from then on, so nobody can accidentally overwrite
   already-submitted marks/attendance. To correct a mistake, an admin edits
   the `Locked` cell for that row in the Google Sheet directly (clear it or
   set to `No`), which unlocks it for the TA to resubmit. Saving always
   updates each student's row in place, so the roster order never changes.

## 2. Create a Google Service Account (so the app can read/write the sheet)

1. Go to https://console.cloud.google.com/ → create a project (or use one
   you have).
2. Enable the **Google Sheets API** and **Google Drive API** for that project.
3. Go to "IAM & Admin" → "Service Accounts" → "Create Service Account".
4. Once created, open it → "Keys" tab → "Add Key" → "Create new key" → JSON.
   This downloads a `.json` file — keep it safe, it's a credential.
5. Open your Google Sheet → click "Share" → paste the service account's
   email address (looks like `something@project-id.iam.gserviceaccount.com`,
   found inside the JSON file) → give it **Editor** access.

## 3. Run it locally first (recommended)

```bash
cd ta_portal
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

**Set up your `.env` file once, so you never have to set environment
variables by hand every time you open a new terminal:**

1. Copy `.env.example` to a new file named `.env` (same folder as `app.py`).
2. Open `.env` and fill in:
   - `GOOGLE_SHEET_ID` — the ID from your sheet's URL
   - `GOOGLE_CREDENTIALS_FILE` — the full path to your downloaded
     service-account `.json` key file (easiest for local use — no need to
     paste the JSON contents anywhere)
   - `SECRET_KEY` — any random string
3. Save it. Both `app.py` and `init_sheet.py` automatically read this file
   every time they run — no `export`/`$env:` needed anymore.

`.env` is already listed in `.gitignore` so it won't accidentally get
committed to git — don't share it, it points to your credentials.

Now create the sheet tabs automatically:
```bash
python init_sheet.py
```

Start the app:
```bash
python app.py
```

Visit http://localhost:5000, log in with a TA email/password you added to
the `TAs` tab, and confirm a new tab appears in the sheet for today's
date with that TA's students filled in.

Optional one-time helper to also import existing lists:
```bash
python init_sheet.py --tas tas.csv --students students.csv
```
(`tas.csv` columns: `TA_ID,TA_Name,Email,Password` — `students.csv` columns:
`StudentID,StudentName,StudentEmail,TA_Email`)

## 4. Deploy on Render

1. Push this folder to a GitHub repo. (`.env` is gitignored — it won't be
   pushed, which is correct: Render needs the credentials as real
   dashboard env vars instead, set up next.)
2. In Render: **New → Web Service** → connect the repo.
3. Build command: `pip install -r requirements.txt`
   Start command: `gunicorn app:app` (already set via `Procfile`, Render
   should detect it automatically).
4. Add environment variables in the Render dashboard:
   - `GOOGLE_SHEET_ID` = your sheet id
   - `GOOGLE_CREDENTIALS_JSON` = paste the **entire contents** of the
     service account JSON file as one value (Render has no file to point
     `GOOGLE_CREDENTIALS_FILE` at, so use the JSON-contents option here)
   - `SECRET_KEY` = any random string
5. Deploy. Render gives you a URL like `https://ta-portal.onrender.com` —
   share that with your TAs. It works fine from a phone browser.

## Notes / things worth knowing

- **Passwords are stored in plain text in the `TAs` sheet.** That's fine for
  a small internal tool where only you and the TAs see the sheet, but don't
  reuse a sensitive password there. If you want proper hashing later, that's
  a straightforward upgrade — just ask.
- Each date's tab keeps only the latest saved value per student (no
  duplicate rows). **Once saved, a student's row is locked** and the TA
  can't resubmit it from the form — this prevents accidental overwrites.
  To fix a mistake, open the sheet, find the row, and clear the `Locked`
  cell (or set it to `No`); the TA can then edit and resave that student.
- Google Sheets has a soft limit on total tabs per spreadsheet (a few
  thousand) — with one tab per day that's years of runway, but if you
  ever run this for a very long time, consider archiving old session tabs
  into a separate spreadsheet periodically.
- Free-tier Render services sleep after inactivity and take ~30–50 seconds
  to wake up on the first request. Fine for occasional use by 10 TAs; if
  that's annoying, a paid Render instance removes the delay.
- Each TA's dashboard is filtered purely by matching their login email to
  the `TA_Email` column in `Students` — so reassigning a student to a
  different TA is just editing one cell in the sheet.
