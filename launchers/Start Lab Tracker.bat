@echo off
setlocal

cd /d "%~dp0\.."

where lab-tracker >nul 2>nul
if %ERRORLEVEL%==0 (
  lab-tracker serve
  exit /b %ERRORLEVEL%
)

where uv >nul 2>nul
if %ERRORLEVEL%==0 (
  uv run lab-tracker serve
  exit /b %ERRORLEVEL%
)

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m lab_tracker serve
  exit /b %ERRORLEVEL%
)

python -m lab_tracker serve
exit /b %ERRORLEVEL%
