@echo off
title LinkedIn Referral Bot
echo.
echo  ========================================
echo   LinkedIn Job Referral Bot - Launcher
echo  ========================================
echo.

:: Close any existing Chrome instances using the bot profile
echo [1/3] Closing Chrome bot instances...
taskkill /F /IM chrome.exe >nul 2>&1
timeout /t 2 /nobreak >nul

:: Clean previous run logs (keep jobs.db for contact tracking)
echo [2/3] Cleaning previous logs...
del /q "%~dp0data\logs\bot.log" >nul 2>&1
del /q "%~dp0data\debug_page.*" >nul 2>&1

:: Launch the bot
echo [3/3] Starting bot...
echo.
echo  Bot is running! Check data\logs\bot.log for progress.
echo  Close this window to stop the bot.
echo.

cd /d "%~dp0"
"C:\Users\Harsh\Desktop\Projects\.venv\Scripts\python.exe" main.py

echo.
echo  Bot finished. Press any key to exit.
pause >nul
