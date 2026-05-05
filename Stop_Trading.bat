@echo off
echo Stopping Paper Trading System...
taskkill /f /im pythonw.exe /fi "WINDOWTITLE eq main.py*" 2>nul
taskkill /f /im pythonw.exe 2>nul
echo.
echo If Python was running, it has been stopped.
pause
