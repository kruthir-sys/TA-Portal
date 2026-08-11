# TA Evaluation - Existing Render Service

Environment variables:
- SECRET_KEY
- GOOGLE_SHEET_ID
- GOOGLE_CREDENTIALS_JSON
- TA_PASSWORD
- INSTRUCTOR_PASSWORD

TA and Instructor passwords are read from Render environment variables.
If the variables are missing during local development, the code falls back to ta123 and instr123 respectively.

Google worksheets:
Students
Marks
TA_List
Instructors
TA_Attendance

Marks:
Student_ID | Student_Name | Marks | TA | Date

TA_Attendance:
TA_Name | Date | Status

Build:
pip install -r requirements.txt

Start:
gunicorn --workers 1 --worker-class gthread --threads 8 --timeout 120 --access-logfile - --error-logfile - app:app
