@echo off
cd /d "%~dp0"
python tools\configure_api_key.py
if errorlevel 1 pause
