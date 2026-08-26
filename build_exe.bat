@echo off
setlocal enableDelayedExpansion
title MailAgent Build

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

set "PYTHON="
if exist "%ROOT%\.venv\Scripts\python.exe" (
    set "PYTHON=%ROOT%\.venv\Scripts\python.exe"
    echo [INFO] Using project venv: !PYTHON!
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

echo [INFO] Running build script...
"%PYTHON%" "%ROOT%\scripts\build.py"

if !errorlevel! NEQ 0 (
    echo.
    echo [ERROR] Build failed.
    pause
    exit /b !errorlevel!
)

echo.
pause
