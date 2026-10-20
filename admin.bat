@echo off
REM Local admin: sessions, run/continue scrapes, analytics (opens in your browser)
cd /d "%~dp0"
python admin_server.py
pause
