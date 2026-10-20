@echo off
REM Installs the packages of requirements.txt (exact versions from constraints.txt) when either file
REM changed since the last install, so an update that adds a package never fails with an ImportError.
REM Called by install.bat, run.bat and dev.bat. Pass "force" to install whatever happened before.
setlocal
cd /d "%~dp0"
if /I "%~1"=="force" goto :install
".venv\Scripts\python" -c "import hashlib,pathlib,sys; h=hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()+pathlib.Path('constraints.txt').read_bytes()).hexdigest(); s=pathlib.Path('.venv/requirements.stamp'); sys.exit(0 if s.exists() and s.read_text().strip()==h else 1)"
if not errorlevel 1 exit /b 0
echo requirements.txt or constraints.txt changed: installing the packages...

:install
".venv\Scripts\python" -m pip install --quiet -r requirements.txt -c constraints.txt
if errorlevel 1 goto :fail
".venv\Scripts\python" -c "import hashlib,pathlib; pathlib.Path('.venv/requirements.stamp').write_text(hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()+pathlib.Path('constraints.txt').read_bytes()).hexdigest())"
exit /b 0

:fail
echo.
echo Could not install the packages (see the error above). Check the internet connection, then run install.bat.
exit /b 1
