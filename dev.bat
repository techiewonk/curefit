@echo off
REM Development: the web server restarts when a .py file changes (a running scrape is not affected,
REM it lives in the job runner). Sets itself up first if needed and applies pending migrations.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    call install.bat nopause
    if errorlevel 1 goto :fail
)
set CUREFIT_DEBUG=1
".venv\Scripts\python" manage.py migrate --noinput
if errorlevel 1 goto :fail
".venv\Scripts\python" manage.py seed_groups
if errorlevel 1 goto :fail
".venv\Scripts\python" manage.py collectstatic --noinput --verbosity 0
if errorlevel 1 goto :fail
".venv\Scripts\python" manage.py serve --reload
pause
exit /b 0

:fail
echo.
echo Could not start. Fix the error above, or run install.bat.
pause
exit /b 1
