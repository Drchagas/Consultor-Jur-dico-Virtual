$ErrorActionPreference='Continue'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new() } catch {}
$Root=Join-Path $env:LOCALAPPDATA 'JARBAS_Enterprise'
$Desktop=[Environment]::GetFolderPath('Desktop')
$Out=Join-Path $Desktop 'JARBAS_DIAGNOSTICO_8_3_0.txt'
$lines=New-Object System.Collections.Generic.List[string]
function Add([string]$s){$lines.Add($s)}
Add 'JARBAS Juridico Enterprise 9.1.0 - Diagnostico'
Add "Data: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Add "Windows: $([Environment]::OSVersion.VersionString)"
Add "Root: $Root"
$Python=Join-Path $Root 'runtime\python.exe'
$EnvFile=Join-Path $Root '.env.local'
if(Test-Path $Python){try{Add "Python: $(& $Python --version 2>&1)"}catch{Add "Python ERRO: $($_.Exception.Message)"}}else{Add 'Python: AUSENTE'}
$Pth=Get-ChildItem (Join-Path $Root 'runtime') -Filter 'python*._pth' -ErrorAction SilentlyContinue|Select-Object -First 1
if($Pth){# O build responde "qual pacote esta instalado?". Sem ele, dois pacotes
# diferentes se apresentam como 9.1.0 e a pergunta fica sem resposta.
$BuildFile=Join-Path $Root 'BUILD.txt'
if(Test-Path $BuildFile){Add '--- build instalado ---';Get-Content $BuildFile|Select-Object -First 3|ForEach-Object{Add $_}}
else{Add '--- build instalado ---';Add 'BUILD.txt AUSENTE: instalacao anterior a este controle, ou extracao incompleta.'}
Add '--- python _pth ---';Get-Content $Pth.FullName|ForEach-Object{Add $_}}
$Port=''
# Qualquer chave e mascarada por PADRAO. A lista anterior citava
# OPENAI_API_KEY, que a 9.0 nem usa, e deixava a ANTHROPIC_API_KEY passar
# inteira - num arquivo cuja finalidade e ser enviado ao suporte.
if(Test-Path $EnvFile){
  Add '--- configuracao (segredos omitidos) ---'
  Get-Content $EnvFile|ForEach-Object{
    if($_ -match '^\s*([A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|SENHA)[A-Z0-9_]*)\s*=\s*(.*)$'){
      $nome=$Matches[1];$valor=$Matches[3]
      if($valor.Trim()){Add ($nome+'=[CONFIGURADA/OCULTA, '+$valor.Trim().Length+' caracteres]')}
      else{Add ($nome+'=[VAZIA]')}
    }else{Add $_}
    if($_ -match '^JARBAS_PORT=(\d+)'){$Port=$Matches[1]}
  }
}
# Variavel de ambiente do Windows VENCE o .env.local: o app usa
# load_dotenv(override=False). Sem esta checagem, o operador corrige o arquivo,
# nada muda, e nao ha como adivinhar por que.
Add '--- chave de IA no ambiente do Windows (vence o .env.local) ---'
foreach($escopo in @('Process','User','Machine')){
  try{
    $v=[Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY',$escopo)
    if($v){Add ("ANTHROPIC_API_KEY em "+$escopo+" = [DEFINIDA, "+$v.Trim().Length+" caracteres, comeca com '"+$v.Trim().Substring(0,[Math]::Min(7,$v.Trim().Length))+"']")}
    else{Add ("ANTHROPIC_API_KEY em "+$escopo+" = (nao definida)")}
  }catch{Add ("ANTHROPIC_API_KEY em "+$escopo+" = (nao foi possivel ler)")}
}
if(Test-Path $Python){
 Add '--- sys.path / imports ---'
 $env:JARBAS_ROOT=$Root
 try{(& $Python -c "import sys; print('SYS_PATH='+repr(sys.path)); import app.main; print('IMPORT_APP_OK'); import anthropic; print('ANTHROPIC_SDK='+getattr(anthropic,'__version__','?'))" 2>&1)|ForEach-Object{Add "$_"}}catch{Add "IMPORT ERRO: $($_.Exception.Message)"}
 Add '--- banco ---'
 try{(& $Python (Join-Path $Root 'tools\diagnose_db.py') 2>&1)|ForEach-Object{Add "$_"}}catch{Add "DB CHECK ERRO: $($_.Exception.Message)"}
 Add '--- diagnostico de PDFs / OCR / OpenAI ---'
 try{(& $Python (Join-Path $Root 'tools\diagnose_pdfs.py') 2>&1)|ForEach-Object{Add "$_"}}catch{Add "PDF CHECK ERRO: $($_.Exception.Message)"}
}
if($Port){Add "Porta: $Port";try{$r=Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$Port/health" -TimeoutSec 3;Add "Health: HTTP $($r.StatusCode) $($r.Content)"}catch{Add "Health ERRO: $($_.Exception.Message)"};Add '--- netstat ---';(& netstat -ano|Select-String ":$Port")|ForEach-Object{Add $_.Line}}
foreach($f in @('instalacao-9.1.0.log','bootstrap-admin-9.1.0.log','jarbas-error.log','jarbas-out.log','runtime-errors.log','prestart-import.log')){Add "--- $f ---";$p=Join-Path $Root "logs\$f";if(Test-Path $p){Get-Content $p -Tail 350|ForEach-Object{Add $_}}else{Add '(ausente)'}}
[IO.File]::WriteAllLines($Out,$lines,[Text.UTF8Encoding]::new($true))
Start-Process notepad.exe $Out
