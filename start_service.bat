@echo off
setlocal enableDelayedExpansion
title MailAgent Service

:: ── Auto-detect project root (the folder containing this bat file) ──────────
set "ROOT=%~dp0"
:: Remove trailing backslash
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

cd /d "%ROOT%"

:: ── Find Python ──────────────────────────────────────────────────────────────
:: Priority: .venv (project venv) > py launcher > python in PATH
set "PYTHON="
if exist "%ROOT%\.venv\Scripts\python.exe" (
    set "PYTHON=%ROOT%\.venv\Scripts\python.exe"
    echo [INFO] Using project venv: %PYTHON%
) else (
    where py >nul 2>&1
    if !errorlevel!==0 (
        set "PYTHON=py"
        echo [INFO] Using py launcher.
    ) else (
        where python >nul 2>&1
        if !errorlevel!==0 (
            set "PYTHON=python"
            echo [INFO] Using python in PATH.
        ) else (
            echo [ERROR] Python not found. Install Python 3.10+ or create a .venv.
            pause
            exit /b 1
        )
    )
)

:: ── Check & install dependencies if missing ─────────────────────────
echo [INFO] Verifying Python dependencies...
"%PYTHON%" -c "import aiohttp, pydantic, loguru, playwright, requests, webview" >nul 2>&1
if !errorlevel! NEQ 0 (
    echo [INFO] Installing required dependencies from requirements.txt...
    "%PYTHON%" -m pip install -r "%ROOT%\requirements.txt" --disable-pip-version-check
    if !errorlevel! NEQ 0 (
        echo [WARN] Pip install had issues. Attempting to proceed...
    )
    echo [INFO] Ensuring Playwright Chromium browser is installed...
    "%PYTHON%" -m playwright install chromium
)

:: ── Restart settings ─────────────────────────────────────────────────────────
set RESTART_DELAY=15
set MAX_CRASHES=10
set CRASH_COUNT=0

:RESTART
set /a CRASH_COUNT+=1

if %CRASH_COUNT% GTR %MAX_CRASHES% (
    echo.
    echo [%date% %time%] Too many crashes ^(%CRASH_COUNT%^). Stopping auto-restart.
    echo [%date% %time%] Check logs\mailagent.log for details.
    goto END
)

echo.
echo ============================================================
echo  [%date% %time%] Starting MailAgent  ^(attempt #%CRASH_COUNT%^)
echo ============================================================

:: Run main.py — the UI server + browser open + workers all start from here
"%PYTHON%" "%ROOT%\main.py"
set EXIT_CODE=!errorlevel!

echo.
echo [%date% %time%] Process exited with code %EXIT_CODE%.

:: Exit code 0 = clean shutdown (user closed / stopped via UI)
if %EXIT_CODE%==0 (
    echo [%date% %time%] Clean exit. Service stopped.
    goto END
)

:: Exit code 1 with first crash = likely a config/setup error, still restart
echo [%date% %time%] Unexpected exit. Restarting in %RESTART_DELAY% seconds...
echo [%date% %time%] Press Ctrl+C to cancel restart.
timeout /t %RESTART_DELAY% /nobreak >nul
goto RESTART

:END
echo.
echo [%date% %time%] MailAgent Service stopped.
pause
