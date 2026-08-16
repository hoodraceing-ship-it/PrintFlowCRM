@echo off
cd /d "%~dp0"
where pyw.exe >nul 2>nul
if %errorlevel%==0 (
  start "" pyw.exe -3 "%~dp0PrintFlowCRM.pyw"
  exit /b
)
where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw.exe "%~dp0PrintFlowCRM.pyw"
  exit /b
)
echo Python was not found. Install Python 3 and try again.
pause
