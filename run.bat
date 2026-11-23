@echo off
setlocal
chcp 65001 >nul
title Faran Trading Intelligence

cd /d "%~dp0"

echo ==========================================
echo       FARAN TRADING INTELLIGENCE
echo ==========================================
echo.

REM Check Python installation
where py >nul 2>&1
if not errorlevel 1 goto USE_PY

where python >nul 2>&1
if not errorlevel 1 goto USE_PYTHON

echo ERROR: Python is not installed or not in PATH.
echo Please install Python 3.10 or newer from https://python.org
pause
exit /b 1

:USE_PY
set "PY=py"
goto CHECK_VERSION

:USE_PYTHON
set "PY=python"

:CHECK_VERSION
echo Using: %PY%
%PY% --version
echo.

:INSTALL
echo Checking and installing required libraries...
echo.

%PY% -m pip install --upgrade pip --quiet
if errorlevel 1 goto ERROR

%PY% -m pip install -r requirements.txt --quiet
if errorlevel 1 goto ERROR

echo Libraries installed successfully.
echo.
echo ==========================================
echo Faran Trading Intelligence is starting...
echo ==========================================
echo.
echo Open this address in Chrome or Edge:
echo http://127.0.0.1:8000
echo.
echo Press CTRL+C to stop the server.
echo.

%PY% -m uvicorn main:app --host 127.0.0.1 --port 8000

echo.
echo Server stopped.
pause
exit /b 0

:ERROR
echo.
echo ERROR: Failed to install required libraries.
echo Check Python installation and internet connection.
pause
exit /b 1
