$ErrorActionPreference='Stop'
$Root=Join-Path $env:LOCALAPPDATA 'JARBAS_Enterprise';if(-not(Test-Path $Root)){Write-Host 'JARBAS nao encontrado.';exit 0}
$keep=Read-Host 'Deseja preservar backup de dados/documentos antes de remover? (S/N)';if($keep -match '^[Ss]'){& (Join-Path $Root 'BACKUP_JARBAS.ps1')}
& (Join-Path $Root 'PARAR_JARBAS.ps1');Start-Sleep -Seconds 1
$desktop=[Environment]::GetFolderPath('Desktop');Get-ChildItem $desktop -Filter 'JARBAS*.lnk' -ErrorAction SilentlyContinue|Remove-Item -Force -ErrorAction SilentlyContinue;$startDir=Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\JARBAS Juridico';if(Test-Path $startDir){Remove-Item $startDir -Recurse -Force -ErrorAction SilentlyContinue};Remove-Item $Root -Recurse -Force;Write-Host 'JARBAS removido.' -ForegroundColor Green
