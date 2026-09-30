@echo off
cd /d "%~dp0"
title Video Keyframe Tool - Build portable exe
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Environment not found. Please double-click setup.bat once first.
    pause
    exit /b 1
)
echo Building the portable version (takes a few minutes) ...
".venv\Scripts\python.exe" build_exe.py
if errorlevel 1 (
    echo.
    echo Build failed. See the messages above.
    pause
    exit /b 1
)
echo.
echo Finished. The portable tool is in dist\KeyframeTool - zip that folder to share it.
pause
