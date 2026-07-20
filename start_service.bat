@echo off
setlocal enableDelayedExpansion
title MailAgent Service
cd /d "C:\Users\AdamZheng\OneDrive - TP-Link\MailAgentWin"

set RESTART_DELAY=15
set CRASH_COUNT=0

:RESTART
set /a CRASH_COUNT+=1
echo ============================================
echo [%date% %time%] Starting MailAgent (attempt #%CRASH_COUNT%)
echo ============================================

python "C:\Users\AdamZheng\OneDrive - TP-Link\MailAgentWin\main.py"
set EXIT_CODE=%errorlevel%

echo.
echo [%date% %time%] Process exited with code %EXIT_CODE%.

if %EXIT_CODE%==0 (
    echo [%date% %time%] Clean exit detected. Stopping service.
    goto END
)

echo [%date% %time%] Restarting in %RESTART_DELAY% seconds...
timeout /t %RESTART_DELAY% /nobreak >nul
goto RESTART

:END
echo [%date% %time%] MailAgent Service stopped.
pause
