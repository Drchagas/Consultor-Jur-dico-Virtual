@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Este pacote ZIP e o metodo recomendado para distribuicao de teste.
echo Para gerar um EXE comercial assinado, use uma ferramenta de empacotamento como Inno Setup/WiX no Windows e um certificado Code Signing.
echo O codigo-fonte do instalador esta em INSTALAR_JARBAS_9_0_2.ps1.
pause
