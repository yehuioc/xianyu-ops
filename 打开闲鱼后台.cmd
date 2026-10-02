@echo off
cd /d "%~dp0"
python -X utf8 -B scripts\xianyu-console.py start
if errorlevel 1 pause
if not errorlevel 1 start http://127.0.0.1:8090
