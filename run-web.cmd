@echo off
setlocal EnableExtensions
cd /d "%~dp0"

where powershell.exe >nul 2>&1
if errorlevel 1 exit /b 1

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-web.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo Web launcher failed. Exit code: %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
