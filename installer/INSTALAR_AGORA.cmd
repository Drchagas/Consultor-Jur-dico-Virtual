@echo off
setlocal
chcp 65001 >nul
color 0B
title JARBAS Juridico Enterprise 9.1.0 - Instalador Completo
cd /d "%~dp0"
echo.
echo ========================================================================
echo  JARBAS JURIDICO ENTERPRISE 9.1.0 - AUDITED BUILD
echo ========================================================================
echo.
echo IMPORTANTE: extraia todo o ZIP antes de executar este arquivo.
echo O instalador faz backup das versoes anteriores antes de migrar os dados.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0INSTALAR_JARBAS_9_1_0.ps1"
set RC=%ERRORLEVEL%
echo.
if "%RC%"=="0" (
 color 0A
 echo Instalacao concluida, testada e validada.
) else (
 color 0C
 echo Instalacao terminou com erro %RC%.
 echo Consulte o diagnostico e o log gerados automaticamente.
)
echo.
pause
exit /b %RC%
