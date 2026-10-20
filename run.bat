@echo off
REM One-command auto flow: login (OTP) -> ask dates -> scrape -> dashboard
cd /d "%~dp0"
python run.py
pause
