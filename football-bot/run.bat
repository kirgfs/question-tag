@echo off
setlocal
cd /d "%~dp0"

rem ---- find Python 3.12-3.14 ----
set "PY="
for %%V in (3.13 3.12 3.14) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if (3,12) <= sys.version_info[:2] <= (3,14) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python 3.12, 3.13 or 3.14 is required: https://www.python.org/downloads/
  echo Tick "Add python.exe to PATH" during installation.
  pause
  exit /b 1
)

rem ---- virtual environment and dependencies ----
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment .venv ...
  %PY% -m venv .venv || goto :fail
)
fc /b requirements.txt ".venv\requirements.installed" >nul 2>nul
if errorlevel 1 (
  echo Installing dependencies, this takes a few minutes the first time ...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt || goto :fail
  copy /y requirements.txt ".venv\requirements.installed" >nul
)

rem ---- start the site (extra arguments are passed through, e.g. --host 0.0.0.0) ----
".venv\Scripts\python.exe" -m fbot %*
pause
exit /b 0

:fail
echo.
echo Something went wrong, see the messages above.
pause
exit /b 1
