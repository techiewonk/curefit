@echo off
REM Start the admin (web server + job runner) and open it in the browser.
REM Sets itself up first if needed, and applies any pending database migrations.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    call install.bat nopause
    if errorlevel 1 goto :fail
)
".venv\Scripts\python" manage.py migrate --noinput
if errorlevel 1 goto :fail
".venv\Scripts\python" manage.py serve
pause
exit /b 0

:fail
echo.
echo Could not start. Fix the error above, or run install.bat.
pause
exit /b 1
