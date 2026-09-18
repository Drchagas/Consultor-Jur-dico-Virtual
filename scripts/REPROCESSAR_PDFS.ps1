$ErrorActionPreference='Stop'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new() } catch {}
$Root=Join-Path $env:LOCALAPPDATA 'JARBAS_Enterprise'
$Python=Join-Path $Root 'runtime\python.exe'
if(-not(Test-Path $Python)){Write-Host 'Runtime Python do JARBAS não encontrado.' -ForegroundColor Red;exit 2}
$Stop=Join-Path $Root 'PARAR_JARBAS.ps1';$Start=Join-Path $Root 'INICIAR_JARBAS.ps1'
Write-Host 'JARBAS 8.3 - Reprocessamento integral dos PDFs' -ForegroundColor Cyan
Write-Host 'O sistema será reiniciado ao final. Nenhum PDF original será apagado.' -ForegroundColor Gray
if(Test-Path $Stop){& $Stop | Out-Null}
$env:JARBAS_ROOT=$Root
& $Python (Join-Path $Root 'tools\reindex_pdfs.py')
$rc=$LASTEXITCODE
if(Test-Path $Start){& $Start}
if($rc -ne 0){Write-Host 'Alguns PDFs apresentaram falha. Execute DIAGNOSTICO_JARBAS.cmd para ver o motivo.' -ForegroundColor Yellow;exit $rc}
Write-Host 'Todos os PDFs elegíveis foram reprocessados.' -ForegroundColor Green
exit 0
