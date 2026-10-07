@echo off
REM Sentinel-X - demarrage des services du PC hote (double-clic)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
echo.
pause
