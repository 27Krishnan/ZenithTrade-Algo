@echo off
set /p pass=Enter password:

if "%pass%"=="12345" (
attrib -r *.py
echo %date% %time% - UNLOCKED >> lock_log.txt
echo Files unlocked ✅
) else (
echo Wrong password ❌
)

pause
