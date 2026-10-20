@echo off
REM Development: the web server restarts when a .py file changes (running scrapes are not affected).
cd /d "%~dp0"
set CUREFIT_DEBUG=1
.venv\Scripts\python manage.py serve --reload
pause
