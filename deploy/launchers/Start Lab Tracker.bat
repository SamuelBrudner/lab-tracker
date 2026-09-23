@echo off
setlocal

cd /d "%~dp0\..\.."

rem cmd.exe expands %ERRORLEVEL% when it parses a whole ( ... ) block, before
rem any command in it runs, so the serve status is read only after the block.
rem "if errorlevel 1" is evaluated at run time and is safe inside blocks.
where lab-tracker >nul 2>nul
if not errorlevel 1 (
  lab-tracker serve
) else (
  where uv >nul 2>nul
  if not errorlevel 1 (
    uv run lab-tracker serve
  ) else if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m lab_tracker serve
  ) else (
    python -m lab_tracker serve
  )
)
set "status=%ERRORLEVEL%"

if not "%status%"=="0" (
  echo.
  echo Lab Tracker stopped with exit code %status%.
  echo If Lab Tracker is not installed in this checkout yet, install uv from
  echo https://docs.astral.sh/uv/ and then double-click this launcher again.
  echo.
  pause
)
exit /b %status%
