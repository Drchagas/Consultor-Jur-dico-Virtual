@echo off
setlocal
cd /d "%~dp0.."
if "%~1"=="" (
  echo.
  echo  Arraste o arquivo PDF para cima deste .cmd, ou rode:
  echo    DIAGNOSTICAR_PDF.cmd "C:\caminho\do\arquivo.PDF"
  echo.
  pause
  exit /b 1
)
"%~dp0..\runtime\python.exe" "tools\diagnosticar_pdf.py" %*
echo.
pause
