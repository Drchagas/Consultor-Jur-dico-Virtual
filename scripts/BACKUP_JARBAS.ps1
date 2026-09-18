$ErrorActionPreference='Stop'
# Backup do JARBAS no Windows.
#
# A versao anterior fazia Copy-Item da pasta data\ inteira. Copiar um SQLite
# enquanto o servidor escreve produz um arquivo com paginas de instantes
# diferentes, que o proprio SQLite recusa a abrir — e isso so aparece no dia
# em que for preciso restaurar. Agora o trabalho e feito por tools\backup.py,
# que usa a API de backup online do SQLite e CONFERE a copia logo apos gera-la.
$Root=Join-Path $env:LOCALAPPDATA 'JARBAS_Enterprise'
if(-not(Test-Path $Root)){throw 'JARBAS nao instalado.'}
$Python=Join-Path $Root 'runtime\python.exe'
if(-not(Test-Path $Python)){$Python='python'}
$Script=Join-Path $Root 'tools\backup.py'
if(-not(Test-Path $Script)){throw "tools\backup.py nao encontrado em $Root. Atualize a instalacao."}

$Dest=Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'JARBAS_Backups'
New-Item -ItemType Directory -Force -Path $Dest|Out-Null

$env:JARBAS_ROOT=$Root
$env:JARBAS_DATA_DIR=Join-Path $Root 'data'
$env:JARBAS_BACKUP_DIR=$Dest
if(-not $env:JARBAS_BACKUP_MANTER){$env:JARBAS_BACKUP_MANTER='14'}

& $Python $Script
if($LASTEXITCODE -ne 0){
  Write-Host 'BACKUP FALHOU. Nao considere os dados protegidos.' -ForegroundColor Red
  exit $LASTEXITCODE
}

# .env.local guarda a chave de sessao e a chave da IA. Vai junto porque sem
# ele a restauracao nao volta a funcionar — e por isso a pasta de backup
# precisa ficar em local restrito, nunca em nuvem compartilhada sem cifra.
foreach($name in @('.env.local','VERSION.txt')){
  $src=Join-Path $Root $name
  if(Test-Path $src){Copy-Item $src -Destination $Dest -Force}
}
$ws=Join-Path $Root 'app\static\workspaces'
if(Test-Path $ws){Copy-Item $ws -Destination (Join-Path $Dest 'workspaces') -Recurse -Force}

Write-Host "Backup concluido e CONFERIDO em: $Dest" -ForegroundColor Green
Write-Host 'Esta pasta contem autos sob sigilo profissional. Guarde com acesso restrito.' -ForegroundColor Yellow
Start-Process explorer.exe $Dest
