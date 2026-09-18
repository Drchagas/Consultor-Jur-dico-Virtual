$ErrorActionPreference='Continue'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new() } catch {}
$Root=Join-Path $env:LOCALAPPDATA 'JARBAS_Enterprise'
$Desktop=[Environment]::GetFolderPath('Desktop')
$Out=Join-Path $Desktop 'JARBAS_DIAGNOSTICO_8_3_0.txt'
$lines=New-Object System.Collections.Generic.List[string]
function Add([string]$s){$lines.Add($s)}
Add 'JARBAS Juridico Enterprise 9.0.2 - Diagnostico'
Add "Data: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Add "Windows: $([Environment]::OSVersion.VersionString)"
Add "Root: $Root"
$Python=Join-Path $Root 'runtime\python.exe'
$EnvFile=Join-Path $Root '.env.local'
if(Test-Path $Python){try{Add "Python: $(& $Python --version 2>&1)"}catch{Add "Python ERRO: $($_.Exception.Message)"}}else{Add 'Python: AUSENTE'}
$Pth=Get-ChildItem (Join-Path $Root 'runtime') -Filter 'python*._pth' -ErrorAction SilentlyContinue|Select-Object -First 1
if($Pth){Add '--- python _pth ---';Get-Content $Pth.FullName|ForEach-Object{Add $_}}
$Port=''
if(Test-Path $EnvFile){Add '--- configuracao (segredos omitidos) ---';Get-Content $EnvFile|ForEach-Object{if($_ -match '^OPENAI_API_KEY='){Add 'OPENAI_API_KEY=[CONFIGURADA/OCULTA]'}elseif($_ -match '^JARBAS_SECRET_KEY='){Add 'JARBAS_SECRET_KEY=[OCULTA]'}else{Add $_};if($_ -match '^JARBAS_PORT=(\d+)'){$Port=$Matches[1]}}}
if(Test-Path $Python){
 Add '--- sys.path / imports ---'
 $env:JARBAS_ROOT=$Root
 try{(& $Python -c "import sys; print('SYS_PATH='+repr(sys.path)); import app.main; print('IMPORT_APP_OK'); import openai; print('OPENAI_SDK='+openai.__version__)" 2>&1)|ForEach-Object{Add "$_"}}catch{Add "IMPORT ERRO: $($_.Exception.Message)"}
 Add '--- banco ---'
 try{(& $Python (Join-Path $Root 'tools\diagnose_db.py') 2>&1)|ForEach-Object{Add "$_"}}catch{Add "DB CHECK ERRO: $($_.Exception.Message)"}
 Add '--- diagnostico de PDFs / OCR / OpenAI ---'
 try{(& $Python (Join-Path $Root 'tools\diagnose_pdfs.py') 2>&1)|ForEach-Object{Add "$_"}}catch{Add "PDF CHECK ERRO: $($_.Exception.Message)"}
}
if($Port){Add "Porta: $Port";try{$r=Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$Port/health" -TimeoutSec 3;Add "Health: HTTP $($r.StatusCode) $($r.Content)"}catch{Add "Health ERRO: $($_.Exception.Message)"};Add '--- netstat ---';(& netstat -ano|Select-String ":$Port")|ForEach-Object{Add $_.Line}}
foreach($f in @('instalacao-9.0.2.log','bootstrap-admin-9.0.2.log','jarbas-error.log','jarbas-out.log','runtime-errors.log','prestart-import.log')){Add "--- $f ---";$p=Join-Path $Root "logs\$f";if(Test-Path $p){Get-Content $p -Tail 350|ForEach-Object{Add $_}}else{Add '(ausente)'}}
[IO.File]::WriteAllLines($Out,$lines,[Text.UTF8Encoding]::new($true))
Start-Process notepad.exe $Out
