@echo off
title AeroBridge Mock DLL Server

set "PYTHON=python"
cd /d "%~dp0"

where "%PYTHON%" >nul 2>&1 || (
    echo Python was not found on PATH.
    pause
    exit /b 1
)

echo ========================================
echo   AeroBridge - Mock DLL Server
echo   Telemetry : localhost:12345
echo   Command   : localhost:12346
echo ========================================
echo.

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":12345 "') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":12346 "') do taskkill /F /PID %%a >nul 2>&1

echo Starting server...
echo.

"%PYTHON%" -u core\mock_dll_server.py

echo.
echo Server stopped.
pause
