@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $root=(Get-Location).Path; $bad=0; Get-Content '.\PACOTE_MANIFEST_SHA256.txt' ^| ForEach-Object { if($_ -match '^([a-fA-F0-9]{64})\s+(.+)$'){ $expected=$Matches[1].ToLower(); $p=Join-Path $root $Matches[2]; if(-not(Test-Path $p)){Write-Host ('AUSENTE: '+$Matches[2]) -ForegroundColor Red; $bad++} else {$actual=(Get-FileHash -Algorithm SHA256 $p).Hash.ToLower(); if($actual -ne $expected){Write-Host ('CORROMPIDO: '+$Matches[2]) -ForegroundColor Red; $bad++}} }}; if($bad -eq 0){Write-Host 'PACOTE INTEGRO.' -ForegroundColor Green; exit 0}else{Write-Host ($bad.ToString()+' erro(s) de integridade.') -ForegroundColor Red; exit 2}"
pause
