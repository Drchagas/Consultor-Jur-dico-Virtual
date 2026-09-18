@echo off
setlocal
chcp 65001 >nul
color 0B
title JARBAS Juridico Enterprise 9.2.0 - Instalacao
cd /d "%~dp0"

cls
echo.
echo   ------------------------------------------------------------------
echo    JARBAS JURIDICO ENTERPRISE 9.2.0
echo    CHAGAS - ADVOGADOS
echo   ------------------------------------------------------------------
echo.
echo    Este e o unico arquivo que voce precisa abrir.
echo.
echo    Antes de continuar, confirme que o ZIP foi extraido INTEIRO.
echo    Instalar de dentro do ZIP e a causa numero 1 de falha: o Windows
echo    abre uma copia temporaria e o instalador nao acha os arquivos.
echo.

if not exist "%~dp0payload\app" (
  color 0C
  echo   [X] A pasta payload nao foi encontrada ao lado deste arquivo.
  echo.
  echo   Voce provavelmente esta executando de dentro do ZIP.
  echo   Feche esta janela, extraia o ZIP para uma pasta ^(botao direito ^>
  echo   Extrair Tudo^) e abra o INSTALAR.cmd de dentro da pasta extraida.
  echo.
  pause
  exit /b 90
)

if not exist "%~dp0INSTALAR_JARBAS_9_2_0.ps1" (
  color 0C
  echo   [X] O instalador INSTALAR_JARBAS_9_2_0.ps1 nao esta nesta pasta.
  echo   Extraia o ZIP novamente, sem selecionar arquivos avulsos.
  echo.
  pause
  exit /b 91
)

echo   Tudo certo. A instalacao vai comecar.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0INSTALAR_JARBAS_9_2_0.ps1"
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
  color 0A
  echo   ------------------------------------------------------------------
  echo    PRONTO. O JARBAS esta instalado e ja abriu no navegador.
  echo    O atalho "JARBAS Juridico 9.2.0" ficou na area de trabalho.
  echo   ------------------------------------------------------------------
) else (
  color 0C
  echo   ------------------------------------------------------------------
  echo    A instalacao parou ^(codigo %RC%^).
  echo    Nenhum dado anterior foi apagado: a copia de seguranca fica em
  echo    Documentos\JARBAS_Backups.
  echo    A janela acima mostra o motivo e o caminho do log.
  echo   ------------------------------------------------------------------
)
echo.
pause
exit /b %RC%
