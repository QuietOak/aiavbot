@echo off
REM Starts AIAVBOT. First run creates a Python environment in %LOCALAPPDATA%\AIAVBOT\venv
REM (outside Dropbox) and installs what the bot needs.
cd /d "%~dp0"
set VENV=%LOCALAPPDATA%\AIAVBOT\venv

if not exist "%VENV%\Scripts\python.exe" (
    echo Setting up Python environment, one moment...
    py -3 -m venv "%VENV%" || python -m venv "%VENV%"
)
"%VENV%\Scripts\python.exe" -m pip install -q -r requirements.txt

echo Starting AIAVBOT. Close this window or press Ctrl+C to stop.
"%VENV%\Scripts\python.exe" bot.py
pause
