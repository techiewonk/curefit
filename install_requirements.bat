@echo off
REM ----------------------------------------------------------
REM  Curefit scraper - install Python dependencies
REM ----------------------------------------------------------
cd /d "%~dp0"

echo Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not on PATH.
    echo Install Python 3 from https://www.python.org/downloads/ and retry.
    pause
    exit /b 1
)

echo Upgrading pip...
python -m pip install --upgrade pip

echo Installing requirements...
python -m pip install -r requirements.txt

if errorlevel 1 (
    echo [ERROR] Install failed.
    pause
    exit /b 1
)

echo.
echo Done. Dependencies installed.
echo Next: double-click run.bat  (or run:  python run.py)
echo It will log you in, ask for the OTP and dates, then scrape.
pause
