@echo off
cd /d "%~dp0"
if /I not "%PROCESSOR_ARCHITECTURE%"=="AMD64" (
  echo This launcher is for 64-bit Windows.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1"
if errorlevel 1 (
  echo.
  echo Paradee did not start.
  pause
  exit /b 1
)
