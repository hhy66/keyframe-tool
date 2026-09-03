@echo off
cd /d "%~dp0"
title Video Keyframe Tool - Setup

echo Checking existing environment ...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import cv2, numpy, fastapi, uvicorn" >nul 2>&1
    if not errorlevel 1 (
        echo Environment already installed. Nothing to do.
        pause
        exit /b 0
    )
    echo Existing environment is incomplete - will reinstall dependencies ...
) else (
    echo No environment found - will create one ...
)

set "PY=E:\python\python.exe"
if not exist "%PY%" set "PY=python"
if not exist ".venv\Scripts\python.exe" (
    echo Creating Python virtual environment ...
    "%PY%" -m venv .venv
    if errorlevel 1 goto :fail
)
echo Installing dependencies (numpy / opencv / fastapi) ...
echo Using USTC mirror for speed. Takes about 1 minute ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --timeout 60 -i https://pypi.mirrors.ustc.edu.cn/simple/ numpy opencv-python-headless fastapi uvicorn python-multipart
if errorlevel 1 (
    echo.
    echo Mirror failed, retrying with the default index ...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --timeout 60 numpy opencv-python-headless fastapi uvicorn python-multipart
)
if errorlevel 1 goto :fail
echo.
echo Done! Now double-click the startup bat file to run the tool.
pause
exit /b 0
:fail
echo.
echo Setup failed. Please check your network and try again.
pause
exit /b 1
