@echo off
cd /d "%~dp0"
title Video Keyframe Tool
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Environment not found. Please double-click setup.bat once first.
    pause
    exit /b 1
)
echo Starting keyframe tool ... browser will open http://127.0.0.1:8765
echo Press Ctrl+C in this window to stop the tool.
start "" "http://127.0.0.1:8765"
".venv\Scripts\python.exe" server.py
pause
