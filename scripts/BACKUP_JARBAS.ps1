$ErrorActionPreference='Stop'
$Root=Join-Path $env:LOCALAPPDATA 'JARBAS_Enterprise';if(-not(Test-Path $Root)){throw 'JARBAS nao instalado.'}
$Dest=Join-Path ([Environment]::GetFolderPath('MyDocuments')) ('JARBAS_Backups\manual-'+(Get-Date -Format 'yyyyMMdd-HHmmss'));New-Item -ItemType Directory -Force -Path $Dest|Out-Null
foreach($name in @('data','.env.local','CREDENCIAIS_INICIAIS.txt','CREDENCIAL_DESENVOLVEDOR.txt','VERSION.txt')){$src=Join-Path $Root $name;if(Test-Path $src){Copy-Item $src -Destination $Dest -Recurse -Force}}
$ws=Join-Path $Root 'app\static\workspaces';if(Test-Path $ws){Copy-Item $ws -Destination (Join-Path $Dest 'workspaces') -Recurse -Force}
Write-Host "Backup concluido: $Dest" -ForegroundColor Green;Start-Process explorer.exe $Dest
