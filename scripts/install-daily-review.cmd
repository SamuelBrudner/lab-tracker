@echo off
REM One-click setup for Lab Tracker's daily review on Windows.
REM Double-click this file to register the Scheduled Task, or run it from a
REM terminal. Pass-through args go to install-daily-review.ps1, e.g.:
REM   install-daily-review.cmd -BaseUrl https://lab.example.org
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-daily-review.ps1" %*
echo.
pause
