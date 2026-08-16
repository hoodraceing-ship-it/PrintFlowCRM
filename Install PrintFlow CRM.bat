@echo off
cd /d "%~dp0"
echo Starting PrintFlow CRM guided installer...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-PrintFlowCRM.ps1"
if errorlevel 1 (
  echo.
  echo Installation did not complete. Your existing PrintFlow data was left in place.
  pause
)
