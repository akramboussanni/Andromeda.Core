@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Andromeda Server Setup

set "PY="
for %%V in (3.13 3.12 3.11 3.10) do (
  if not defined PY py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
)
if not defined PY (
  python -c "import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [ERROR] Python 3.10 or newer is required.
  echo Download it from https://www.python.org/downloads/windows/ and select Add Python to PATH.
  pause
  exit /b 1
)

%PY% setup_server.py %*
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
  echo.
  echo Setup did not complete. Fix the error above and run this file again.
  pause
  exit /b %RESULT%
)
echo.
echo Setup complete. Use Start-Andromeda-Server.bat to run the single Core API.
pause
