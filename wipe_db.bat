@echo off
echo ============================================
echo   LinkedIn Job Referral Bot - Database Wipe
echo ============================================
echo.

set "DATA_DIR=%~dp0data"

if exist "%DATA_DIR%\jobs.db" (
    del /f /q "%DATA_DIR%\jobs.db"
    echo [OK] jobs.db deleted.
) else (
    echo [--] jobs.db not found, skipping.
)

if exist "%DATA_DIR%\bot.db" (
    del /f /q "%DATA_DIR%\bot.db"
    echo [OK] bot.db deleted.
) else (
    echo [--] bot.db not found, skipping.
)

echo.
echo Database wiped. Fresh tables will be created on next run.
pause
