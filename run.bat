@echo off
setlocal
title LinkedIn Referral Bot

cd /d "%~dp0"
set "PYTHON=.venv\Scripts\python.exe"

echo.
echo  ========================================
echo   LinkedIn Referral Bot - Launcher
echo  ========================================
echo.

if not exist "%PYTHON%" (
    echo [ERROR] %PYTHON% was not found.
    echo Create the project virtual environment and install requirements first.
    pause
    exit /b 1
)

if exist "data\FROZEN.txt" (
    echo [SAFETY STOP] LinkedIn previously showed a warning:
    type "data\FROZEN.txt"
    echo.
    echo Check the account before deleting data\FROZEN.txt to resume.
    pause
    exit /b 2
)

if "%~1"=="" (
    set "BOT_ARGS=--workday"
    set "PAUSE_WHEN_DONE=1"
    echo Starting workday mode...
    echo It runs short sessions between 08:00 and 17:00 and exits outside those hours.
) else (
    set "BOT_ARGS=%*"
    echo Starting bot with: %*
)

echo.
echo Progress is written to data\logs\bot.log.
echo.

"%PYTHON%" main.py %BOT_ARGS%
set "BOT_EXIT=%ERRORLEVEL%"

if exist "data\FROZEN.txt" (
    echo.
    echo [SAFETY STOP] The bot stopped after a LinkedIn warning. Review data\FROZEN.txt.
) else if "%BOT_EXIT%"=="3" (
    echo.
    echo The bot is already running in the background. Progress: data\logs\bot.log
) else if not "%BOT_EXIT%"=="0" (
    echo.
    echo [ERROR] The bot exited with code %BOT_EXIT%.
) else (
    echo.
    echo Bot finished.
)

if defined PAUSE_WHEN_DONE pause
exit /b %BOT_EXIT%
