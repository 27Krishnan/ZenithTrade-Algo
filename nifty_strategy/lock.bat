@echo off
echo LOCKING FILES...

attrib +r *.py

echo %date% %time% - LOCKED >> lock_log.txt

echo Files locked 🔒
pause
