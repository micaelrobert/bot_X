@echo off
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" (
    start "Telegram para X" ".venv\Scripts\pythonw.exe" "control_panel.py"
    exit /b 0
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "control_panel.py"
    exit /b %errorlevel%
)

py -3.12 "control_panel.py"
