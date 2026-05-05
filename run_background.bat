@echo off
cd /d "%~dp0"
:: Run using pythonw (windowless) directly
"%~dp0venv\Scripts\pythonw.exe" main.py
exit


