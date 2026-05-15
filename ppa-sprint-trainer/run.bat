@echo off
REM PPA Sprint Trainer - one-click launcher (Windows).
REM Starts the desktop coach window + tray icon with no console window.
cd /d "%~dp0"
start "PPA Sprint Trainer" pythonw ppa_app.py
