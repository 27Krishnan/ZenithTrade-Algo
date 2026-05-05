@echo off
echo Stopping existing trading processes...
taskkill /f /im pythonw.exe /fi "WINDOWTITLE eq Papertrading*" 2>nul
taskkill /f /im pythonw.exe 2>nul
echo Starting Trading in Background...
start "" "%~dp0Start_Trading_Silent.vbs"
echo Done.
pause
