@echo off
REM AIAVBOT launcher for Windows startup.
REM Waits for the network after login, then runs the bot and restarts it if it ever stops.
REM Put a SHORTCUT to this file in your Startup folder (Win+R, type shell:startup, Enter).
title AIAVBOT
cd /d "%~dp0"
set VENV=%LOCALAPPDATA%\AIAVBOT\venv

REM Give Windows, the network and Dropbox a moment after login.
echo Waiting 30 seconds for the network...
timeout /t 30 /nobreak >nul

if not exist "%VENV%\Scripts\python.exe" (
    echo Setting up Python environment, one moment...
    py -3 -m venv "%VENV%" || python -m venv "%VENV%"
)
"%VENV%\Scripts\python.exe" -m pip install -q -r requirements.txt

:loop
echo [%date% %time%] Starting AIAVBOT. Close this window to stop it.
"%VENV%\Scripts\python.exe" bot.py
echo [%date% %time%] AIAVBOT stopped. Restarting in 30 seconds...
timeout /t 30 /nobreak >nul
goto loop
