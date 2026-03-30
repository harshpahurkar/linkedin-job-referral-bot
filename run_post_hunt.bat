@echo off
title LinkedIn Post Hunter
echo.
echo  ========================================
echo   LinkedIn Post Hunter - Standalone
echo  ========================================
echo.
echo  Finds hiring posts on LinkedIn, scores
echo  them, and connects with legit posters.
echo.

:: Close any existing Chrome instances using the bot profile
echo [1/3] Closing stale bot Chrome instances...
wmic process where "name='chrome.exe' and commandline like '%%chrome-bot-profiles%%'" call terminate >nul 2>&1
taskkill /F /IM chromedriver.exe >nul 2>&1
timeout /t 2 /nobreak >nul

:: Clean previous run logs
echo [2/3] Cleaning previous logs...
del /q "%~dp0data\logs\bot.log" >nul 2>&1

:: Launch the post hunter
echo [3/3] Starting post hunter...
echo.
echo  Post hunter is running! Check data\logs\bot.log for progress.
echo  Close this window to stop.
echo.

cd /d "%~dp0"
".venv\Scripts\python.exe" run_post_hunt.py

echo.
echo  Post hunter finished. Press any key to exit.
pause >nul
