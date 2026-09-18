@echo off
cd /d "%~dp0.."
"%~dp0..\runtime\python.exe" "tools\diagnosticar_ia.py" %*
echo.
pause
