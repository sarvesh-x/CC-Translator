@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher was not found. Install Python 3.11 from python.org and enable the launcher.
  pause
  exit /b 1
)
py -3.11 -c "import sys; print(sys.version)" >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 is required. Install it from python.org, then run this launcher again.
  pause
  exit /b 1
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo FFmpeg was not found on PATH. Install FFmpeg and add its bin folder to PATH.
  pause
  exit /b 1
)
where ffprobe >nul 2>nul
if errorlevel 1 (
  echo FFprobe was not found on PATH. Install FFmpeg and add its bin folder to PATH.
  pause
  exit /b 1
)
py -3.11 -c "import requests, srt" >nul 2>nul
if errorlevel 1 (
  echo Required Python packages are missing. Run: py -3.11 -m pip install -r requirements.txt
  pause
  exit /b 1
)
py -3.11 main.py
if errorlevel 1 (
  echo The application exited with an error.
  pause
)
endlocal
