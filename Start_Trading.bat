@echo off
setlocal enabledelayedexpansion

:: Change to the directory where the script is located
cd /d "%~dp0"

echo ==========================================
echo   PAPER TRADING SYSTEM - STARTING
echo ==========================================
echo.

:: Define the python executable from the virtual environment
set VENV_PYTHON="%~dp0venv\Scripts\python.exe"

:: Check if venv exists
if not exist %VENV_PYTHON% (
    echo [ERROR] Virtual environment not found at %VENV_PYTHON%
    echo Please ensure the 'venv' folder exists in the project directory.
    pause
    exit /b 1
)

echo [INFO] Using virtual environment: %VENV_PYTHON%
echo [INFO] Dashboard will be available at: http://localhost:8000
echo.
echo Press Ctrl+C to stop the server.
echo ------------------------------------------

:: Run the application
%VENV_PYTHON% main.py

:: If the application crashes, keep the window open
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] The application stopped unexpectedly with error code %ERRORLEVEL%.
    pause
)
