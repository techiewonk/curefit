@echo off
REM Live dashboard that auto-reads curefit.db (opens in your browser)
cd /d "%~dp0"
python dashboard_server.py
pause
