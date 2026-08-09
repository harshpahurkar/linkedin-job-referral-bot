@echo off
title LinkedIn Referral Bot Tests
echo.
echo  ========================================
echo   LinkedIn Referral Bot - Test Suite
echo  ========================================
echo.

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv\Scripts\python.exe not found.
    echo Create the virtualenv and install requirements first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m pytest
set TEST_EXIT=%ERRORLEVEL%

echo.
if "%TEST_EXIT%"=="0" (
    echo Tests passed.
) else (
    echo Tests failed.
)
pause
exit /b %TEST_EXIT%
