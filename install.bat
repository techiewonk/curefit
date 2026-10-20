@echo off
REM One-time setup (safe to run again after an update): Python check, virtual environment,
REM packages, database migrations, system check. Pass "nopause" to skip the final pause.
setlocal
cd /d "%~dp0"
echo === Curefit admin setup ===

echo [1/5] Checking Python (3.12 or newer is needed)...
python -c "import sys; print('Python', sys.version.split()[0]); sys.exit(0 if sys.version_info >= (3, 12) else 1)"
if errorlevel 1 (
    echo.
    echo Python 3.12 or newer was not found. Install it from https://www.python.org/downloads/
    echo and tick "Add Python to PATH", then run this again.
    goto :fail
)

echo [2/5] Virtual environment (.venv)...
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto :fail
) else (
    echo      already there.
)

echo [3/5] Installing packages from requirements.txt...
".venv\Scripts\python" -m pip install --quiet --upgrade pip
".venv\Scripts\python" -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :fail

echo [4/5] Database migrations...
".venv\Scripts\python" manage.py migrate --noinput
if errorlevel 1 goto :fail
".venv\Scripts\python" manage.py seed_groups
if errorlevel 1 goto :fail
".venv\Scripts\python" manage.py collectstatic --noinput --verbosity 0
if errorlevel 1 goto :fail

echo [5/5] System check...
".venv\Scripts\python" manage.py check
if errorlevel 1 goto :fail
if not exist "runs" mkdir "runs"

echo.
echo === Setup finished ===
echo Next:  run.bat           (starts the admin at http://localhost:8000)
echo Then:  Run a scrape ^> send the OTP and enter it  ^>  Update from Kronos (centers)  ^>  pick centers.
if /I not "%~1"=="nopause" pause
exit /b 0

:fail
echo.
echo Setup stopped because of the error above.
if /I not "%~1"=="nopause" pause
exit /b 1
