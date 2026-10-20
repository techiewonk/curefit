@echo off
REM Start the admin (web server + job runner) and open it in the browser.
cd /d "%~dp0"
.venv\Scripts\python manage.py serve
pause
