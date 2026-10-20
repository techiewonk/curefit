@echo off
REM Development: same app, restarts when a .py file changes (running scrapes are not affected)
cd /d "%~dp0"
python webapp.py --reload
pause
