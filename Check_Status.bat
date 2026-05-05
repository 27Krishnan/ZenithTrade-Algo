@echo off
echo Checking Paper Trading System status...
echo.
echo [Processes]
tasklist /fi "imagename eq python.exe" /fo table /nh | findstr "python.exe" > nul
if %ERRORLEVEL% == 0 (echo python.exe is RUNNING) else (echo python.exe is NOT running)

tasklist /fi "imagename eq pythonw.exe" /fo table /nh | findstr "pythonw.exe" > nul
if %ERRORLEVEL% == 0 (echo pythonw.exe is RUNNING) else (echo pythonw.exe is NOT running)

echo.
echo [Port 8000]
netstat -ano | findstr :8000 > nul
if %ERRORLEVEL% == 0 (
    echo Port 8000 is ACTIVE.
    echo System is reachable at: http://localhost:8000
) else (
    echo Port 8000 is CLOSED.
)

echo.
pause

