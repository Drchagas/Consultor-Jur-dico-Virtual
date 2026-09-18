$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new() } catch {}
$SourceDir=$PSScriptRoot
$Payload=Join-Path $SourceDir 'payload'
$ToolsSource=Join-Path $SourceDir 'tools'
$ScriptsSource=Join-Path $SourceDir 'scripts'
$PackageManifest=Join-Path $SourceDir 'PACOTE_MANIFEST_SHA256.txt'
# Raiz canonica e estavel a partir da 8.1: futuras atualizacoes usam a mesma pasta.
$InstallDir=Join-Path $env:LOCALAPPDATA 'JARBAS_Enterprise'
$RuntimeDir=Join-Path $InstallDir 'runtime'
$DataDir=Join-Path $InstallDir 'data'
$LogDir=Join-Path $InstallDir 'logs'
$DownloadDir=Join-Path $InstallDir 'downloads'
$InstallLog=Join-Path $LogDir 'instalacao-9.0.2.log'
$BootstrapLog=Join-Path $LogDir 'bootstrap-admin-9.0.2.log'
$AdminEmail='admin@chagasadvogados.local'
$DeveloperEmail='developer@jarbas.local'

function Say([string]$Text,[string]$Color='Gray'){Write-Host $Text -ForegroundColor $Color}
function Log([string]$Text){$line="$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Text";Add-Content -Path $InstallLog -Value $line -Encoding UTF8}
function Token([int]$Length=48){$alphabet='ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789-_';$bytes=New-Object byte[] $Length;$rng=[Security.Cryptography.RandomNumberGenerator]::Create();try{$rng.GetBytes($bytes)}finally{$rng.Dispose()};return -join($bytes|ForEach-Object{$alphabet[$_ % $alphabet.Length]})}
function SecretPlain([string]$Prompt){$s=Read-Host $Prompt -AsSecureString;$p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s);try{return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($p)}finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p)}}
function Read-NewAdminPassword {for($i=0;$i -lt 5;$i++){$p1=SecretPlain 'Crie a senha ADMIN do JARBAS (minimo 12 caracteres)';$p2=SecretPlain 'Confirme a senha ADMIN';if($p1.Length -ge 12 -and $p1 -ceq $p2){return $p1};Say 'As senhas nao conferem ou possuem menos de 12 caracteres.' Yellow};throw 'Nao foi possivel definir uma senha administrativa valida.'}
function Download-File([string]$Url,[string]$OutFile){Log "Download: $Url";$ok=$false;for($attempt=1;$attempt -le 3 -and -not $ok;$attempt++){try{Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $OutFile -TimeoutSec 240;$ok=$true}catch{try{& curl.exe -L --fail --retry 3 --connect-timeout 30 -o $OutFile $Url;if($LASTEXITCODE -eq 0){$ok=$true}}catch{}}};if(-not $ok -or -not(Test-Path $OutFile) -or (Get-Item $OutFile).Length -lt 1024){throw "Falha ao baixar $Url"}}
function Test-PortFree([int]$Port){try{$l=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,$Port);$l.Start();$l.Stop();return $true}catch{return $false}}
function Find-FreePort {foreach($p in 8765..8799){if(Test-PortFree $p){return $p}};throw 'Nenhuma porta livre entre 8765 e 8799.'}
function EnvValue([string]$File,[string]$Key){if(-not $File -or -not(Test-Path $File)){return ''};$line=Get-Content $File|Where-Object{$_ -match ('^\s*'+[regex]::Escape($Key)+'\s*=')}|Select-Object -First 1;if($line){return $line.Split('=',2)[1]};return ''}
function Merge-Dir([string]$Source,[string]$Destination){if(-not(Test-Path $Source)){return};New-Item -ItemType Directory -Force -Path $Destination|Out-Null;& robocopy.exe $Source $Destination /E /R:2 /W:1 /COPY:DAT /DCOPY:DAT /NFL /NDL /NJH /NJS /NP|Out-Null;if($LASTEXITCODE -ge 8){throw "Falha ao copiar dados de $Source"}}
function Get-CandidateRoots {
  $list=New-Object System.Collections.Generic.List[string]
  foreach($name in @('JARBAS_Enterprise','JARBAS_Enterprise_8_0','JARBAS_Local_4_3','JARBAS_Juridico','JARBAS_Local_4_2','JARBAS_Local_Provisorio_4_1','JARBAS_Local_4_1')){$p=Join-Path $env:LOCALAPPDATA $name;if(Test-Path $p){$list.Add($p)}}
  try{Get-ChildItem $env:LOCALAPPDATA -Directory -Filter 'JARBAS*' -ErrorAction SilentlyContinue|ForEach-Object{if(-not $list.Contains($_.FullName)){$list.Add($_.FullName)}}}catch{}
  return $list
}
function Stop-AllJarbas {
  foreach($r in @(Get-CandidateRoots)){$pf=Join-Path $r 'JARBAS.pid';if(Test-Path $pf){$idText=Get-Content $pf|Select-Object -First 1;if($idText -match '^\d+$'){Stop-Process -Id ([int]$idText) -Force -ErrorAction SilentlyContinue};Remove-Item $pf -Force -ErrorAction SilentlyContinue}}
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue|Where-Object{($_.Name -in @('python.exe','pythonw.exe')) -and $_.CommandLine -and $_.CommandLine -match 'JARBAS'}|ForEach-Object{Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}
  Start-Sleep -Seconds 2
}
function Backup-LegacyRoots([string[]]$Roots){
  $base=Join-Path ([Environment]::GetFolderPath('MyDocuments')) ('JARBAS_Backups\pre-9.0.2-'+(Get-Date -Format 'yyyyMMdd-HHmmss'))
  New-Item -ItemType Directory -Force -Path $base|Out-Null
  foreach($r in $Roots){
    if(-not(Test-Path $r)){continue}
    $name=Split-Path $r -Leaf;$dest=Join-Path $base $name
    New-Item -ItemType Directory -Force -Path $dest|Out-Null
    foreach($item in @('data','.env.local','CREDENCIAIS_INICIAIS.txt','CREDENCIAL_DESENVOLVEDOR.txt')){
      $src=Join-Path $r $item;if(Test-Path $src){Copy-Item $src -Destination $dest -Recurse -Force}
    }
    $ws=Join-Path $r 'app\static\workspaces';if(Test-Path $ws){Copy-Item $ws -Destination (Join-Path $dest 'workspaces') -Recurse -Force}
  }
  Log "Backup legado: $base"
  return $base
}
function Repair-Pth([string]$Runtime){$pth=Get-ChildItem $Runtime -Filter 'python*._pth' -ErrorAction SilentlyContinue|Select-Object -First 1;if(-not $pth){throw 'Arquivo python*._pth nao encontrado no runtime.'};$zipObj=Get-ChildItem $Runtime -Filter 'python*.zip'|Select-Object -First 1;$zipName=if($zipObj){$zipObj.Name}else{'python312.zip'};$lines=@($zipName,'.','Lib\site-packages','..','import site');[IO.File]::WriteAllLines($pth.FullName,$lines,[Text.UTF8Encoding]::new($false));New-Item -ItemType Directory -Force -Path (Join-Path $Runtime 'Lib\site-packages')|Out-Null}
function Verify-Package {
  if(-not(Test-Path $PackageManifest)){throw 'Manifesto SHA-256 do pacote nao encontrado.'}
  foreach($line in Get-Content $PackageManifest){if($line -notmatch '^([a-fA-F0-9]{64})\s+(.+)$'){continue};$expected=$Matches[1].ToLower();$rel=$Matches[2];$p=Join-Path $SourceDir $rel;if(-not(Test-Path $p)){throw "Arquivo ausente no pacote: $rel"};$actual=(Get-FileHash -Algorithm SHA256 $p).Hash.ToLower();if($actual -ne $expected){throw "Arquivo corrompido no pacote: $rel"}}
}
function Write-InstalledManifest {
  $targets=New-Object System.Collections.Generic.List[string]
  foreach($p in Get-ChildItem (Join-Path $InstallDir 'app') -Recurse -File){$targets.Add($p.FullName)}
  foreach($p in Get-ChildItem (Join-Path $InstallDir 'tools') -Recurse -File){$targets.Add($p.FullName)}
  # Enumerar em vez de listar: a lista fixa deixava de fora todo script novo,
  # que passava a nao ser conferido por VERIFICAR_INTEGRIDADE — exatamente o
  # arquivo recem-chegado, que e o mais provavel de vir adulterado ou truncado.
  foreach($p in Get-ChildItem $InstallDir -Filter '*.ps1' -File){$targets.Add($p.FullName)}
  foreach($name in @('requirements.txt','requirements-ia.txt','VERSION.txt')){$p=Join-Path $InstallDir $name;if(Test-Path $p){$targets.Add($p)}}
  $out=New-Object System.Collections.Generic.List[string];foreach($p in $targets){$rel=$p.Substring($InstallDir.Length).TrimStart('\');$hash=(Get-FileHash -Algorithm SHA256 $p).Hash.ToLower();$out.Add("$hash $rel")};[IO.File]::WriteAllLines((Join-Path $InstallDir 'PAYLOAD_MANIFEST_SHA256.txt'),$out,[Text.ASCIIEncoding]::new())
}
function Copy-CurrentBranding([string]$SourceRoot){if(-not $SourceRoot){return};$ws=Join-Path $SourceRoot 'app\static\workspaces';if(Test-Path $ws){Merge-Dir $ws (Join-Path $InstallDir 'app\static\workspaces')}}

New-Item -ItemType Directory -Force -Path $InstallDir,$DataDir,$LogDir,$DownloadDir|Out-Null
Set-Content -Path $InstallLog -Value 'JARBAS 9.0.2 - inicio' -Encoding UTF8
try{
  Say '========================================================================' DarkRed
  Say ' JARBAS JURIDICO ENTERPRISE 9.0.2 - AUDITED BUILD' DarkRed
  Say ' ERP Juridico + Copiloto + Intake PDF + Financeiro + Claude + SaaS' DarkRed
  Say '========================================================================' DarkRed
  Say ''
  if(-not [Environment]::Is64BitOperatingSystem){throw 'Esta distribuicao requer Windows 64 bits.'}

  Say '[1/19] Verificando integridade do pacote...' Cyan;Verify-Package;Say 'Pacote integro.' Green
  Say '[2/19] Encerrando instancias antigas do JARBAS...' Cyan;$legacyRoots=@(Get-CandidateRoots);Stop-AllJarbas
  Say '[3/19] Criando backup de todas as instalacoes encontradas...' Cyan;$BackupRoot=Backup-LegacyRoots $legacyRoots;Say "Backup: $BackupRoot" Green

  Say '[4/19] Instalando nucleo 9.0.2 auditado sem apagar dados...' Cyan
  if(-not(Test-Path $Payload)){throw 'Pasta payload ausente no instalador.'}
  $preserve=Join-Path $env:TEMP ('jarbas-preserve-'+[guid]::NewGuid().ToString('N'));New-Item -ItemType Directory -Force -Path $preserve|Out-Null
  $currentWs=Join-Path $InstallDir 'app\static\workspaces';if(Test-Path $currentWs){Copy-Item $currentWs -Destination (Join-Path $preserve 'workspaces') -Recurse -Force}
  foreach($name in @('app','tools')){$dst=Join-Path $InstallDir $name;if(Test-Path $dst){Remove-Item $dst -Recurse -Force}}
  Copy-Item (Join-Path $Payload 'app') -Destination $InstallDir -Recurse -Force;Copy-Item $ToolsSource -Destination $InstallDir -Recurse -Force;$TestsSource=Join-Path $SourceDir 'tests';if(Test-Path $TestsSource){Copy-Item $TestsSource -Destination $InstallDir -Recurse -Force}
  foreach($f in Get-ChildItem $ScriptsSource -Filter '*.ps1'){Copy-Item $f.FullName (Join-Path $InstallDir $f.Name) -Force}
  foreach($name in @('requirements.txt','requirements-ia.txt','VERSION.txt','LICENSE_PROPRIETARY.txt','README.md','AUDITORIA_8_3.md','AUDITORIA_8_3_1.md','SEGURANCA_LGPD_IA.md','MATRIZ_SISTEMA_PRINCIPAL.md','MATRIZ_FUNCIONAL_7_0.md','ARQUITETURA_SAAS_7_0.md','ROADMAP_PRODUCAO.md','CONSELHO_IA.md','CAPACIDADE_2000_ASSINANTES.md','NOTAS_DA_VERSAO_9_0_2.txt')){$src=Join-Path $Payload $name;if(Test-Path $src){Copy-Item $src -Destination $InstallDir -Force}}
  if(Test-Path (Join-Path $preserve 'workspaces')){Merge-Dir (Join-Path $preserve 'workspaces') (Join-Path $InstallDir 'app\static\workspaces')}

  Say '[5/19] Preparando Python portatil 3.12...' Cyan
  $Python=Join-Path $RuntimeDir 'python.exe'
  if(-not(Test-Path $Python)){
    if(Test-Path $RuntimeDir){Remove-Item $RuntimeDir -Recurse -Force -ErrorAction SilentlyContinue};$runtimeSource=$null
    foreach($r in $legacyRoots){$candidate=Join-Path $r 'runtime\python.exe';if(Test-Path $candidate){$runtimeSource=Join-Path $r 'runtime';break}}
    if($runtimeSource){Say "Reutilizando runtime existente: $runtimeSource" DarkGray;Copy-Item $runtimeSource -Destination $InstallDir -Recurse -Force}else{New-Item -ItemType Directory -Force -Path $RuntimeDir|Out-Null;$zip=Join-Path $DownloadDir 'python-3.12.10-embed-amd64.zip';if(-not(Test-Path $zip)){Download-File 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip' $zip};Expand-Archive $zip $RuntimeDir -Force}
  }
  Repair-Pth $RuntimeDir;& $Python --version;if($LASTEXITCODE -ne 0){throw 'Python portatil nao iniciou.'}

  Say '[6/19] Preparando pip e dependencias fixadas...' Cyan
  $pipOk=$false;try{& $Python -m pip --version *> $null;if($LASTEXITCODE -eq 0){$pipOk=$true}}catch{}
  if(-not $pipOk){$gp=Join-Path $DownloadDir 'get-pip.py';if(-not(Test-Path $gp)){Download-File 'https://bootstrap.pypa.io/get-pip.py' $gp};& $Python $gp --disable-pip-version-check --no-warn-script-location;if($LASTEXITCODE -ne 0){throw 'Falha ao instalar pip no runtime.'}}
  $env:PIP_DISABLE_PIP_VERSION_CHECK='1';$env:PIP_NO_PYTHON_VERSION_WARNING='1';& $Python -m pip install -r (Join-Path $InstallDir 'requirements.txt') --prefer-binary --retries 5 --timeout 90 --no-warn-script-location;if($LASTEXITCODE -ne 0){throw 'Falha ao instalar dependencias. Verifique internet, proxy/antivirus e execute o diagnostico.'}
  # Conselho tri-IA: dependencias OPCIONAIS. Falha aqui NAO aborta a instalacao —
  # o sistema roda sem o provedor correspondente e /conselho informa qual falta.
  $ReqIA=Join-Path $InstallDir 'requirements-ia.txt'
  if(Test-Path $ReqIA){
    Say 'Instalando SDKs do Conselho tri-IA (opcional)...' DarkGray
    & $Python -m pip install -r $ReqIA --prefer-binary --retries 3 --timeout 90 --no-warn-script-location
    if($LASTEXITCODE -ne 0){
      Say 'AVISO: os SDKs de Anthropic/Google nao foram instalados.' Yellow
      Say 'O JARBAS funciona normalmente; o Conselho ficara limitado aos provedores disponiveis.' Yellow
      Say 'Para tentar de novo depois: pip install -r requirements-ia.txt' DarkGray
      $global:LASTEXITCODE=0
    } else { Say 'SDKs do Conselho tri-IA instalados.' Green }
  }

  Say '[7/19] Localizando o banco com os dados reais...' Cyan
  $dbCandidates=New-Object System.Collections.Generic.List[string];foreach($r in @(Get-CandidateRoots)){$d=Join-Path $r 'data\jarbas.db';if(Test-Path $d){$dbCandidates.Add($d)}}
  $BestDb='';if($dbCandidates.Count -gt 0){$scoreOut=& $Python (Join-Path $InstallDir 'tools\score_databases.py') @($dbCandidates);$scoreOut|ForEach-Object{Log $_};$bestLine=$scoreOut|Where-Object{$_ -like 'BEST=*'}|Select-Object -Last 1;if($bestLine){$BestDb=$bestLine.Substring(5)}}
  $SelectedRoot='';if($BestDb){$SelectedRoot=Split-Path (Split-Path $BestDb -Parent) -Parent;Say "Base selecionada: $BestDb" Green}else{Say 'Nenhum banco anterior encontrado; sera criado um banco novo.' DarkGray}

  Say '[8/19] Consolidando banco, PDFs, documentos e identidade visual...' Cyan
  if($SelectedRoot){
    try{$selResolved=(Resolve-Path $SelectedRoot).Path}catch{$selResolved=$SelectedRoot};try{$insResolved=(Resolve-Path $InstallDir).Path}catch{$insResolved=$InstallDir}
    if($selResolved -ne $insResolved){if(Test-Path $DataDir){Remove-Item $DataDir -Recurse -Force};New-Item -ItemType Directory -Force -Path $DataDir|Out-Null;Merge-Dir (Join-Path $SelectedRoot 'data') $DataDir;Copy-CurrentBranding $SelectedRoot;$canonicalDb=Join-Path $DataDir 'jarbas.db';if(Test-Path $BestDb){Copy-Item $BestDb $canonicalDb -Force};& $Python (Join-Path $InstallDir 'tools\rebase_paths.py') $canonicalDb $SelectedRoot $InstallDir|Out-Null}
  }

  Say '[9/19] Criando configuracao segura e preservando a chave do Claude...' Cyan
  $OldEnv='';if(Test-Path (Join-Path $InstallDir '.env.local')){$OldEnv=Join-Path $InstallDir '.env.local'}elseif($SelectedRoot -and (Test-Path (Join-Path $SelectedRoot '.env.local'))){$OldEnv=Join-Path $SelectedRoot '.env.local'}
  $AnthropicKey=EnvValue $OldEnv 'ANTHROPIC_API_KEY';$LegalModel=EnvValue $OldEnv 'JARBAS_AI_MODEL_LEGAL';$IntakeModel=EnvValue $OldEnv 'JARBAS_AI_MODEL_INTAKE';$RoutineModel=EnvValue $OldEnv 'JARBAS_AI_MODEL_ROUTINE';# Migracao da 8.x: o .env.local antigo traz modelos da OpenAI. Preservar
  # esses nomes faz o gateway enviar 'gpt-5.6-sol' para a Anthropic.
  if($LegalModel -notlike 'claude-*'){$LegalModel='claude-opus-5'}
  if($IntakeModel -notlike 'claude-*'){$IntakeModel='claude-sonnet-5'}
  if($RoutineModel -notlike 'claude-*'){$RoutineModel='claude-haiku-4-5-20251001'}
  $Port=Find-FreePort;$Secret=Token 72;$EnvFile=Join-Path $InstallDir '.env.local';$envLines=@('JARBAS_ENV=production',"JARBAS_SECRET_KEY=$Secret","JARBAS_ADMIN_EMAIL=$AdminEmail",'JARBAS_ALLOWED_HOSTS=localhost,127.0.0.1','JARBAS_HTTPS_ONLY=0','JARBAS_PUBLIC_SIGNUP=0','JARBAS_2FA_OBRIGATORIO=0','JARBAS_SESSION_MAX_AGE=28800','JARBAS_TRUSTED_PROXY_HOPS=0','JARBAS_ALLOW_INDEXING=0','JARBAS_ALLOW_SECRET_CONFIG=1','JARBAS_MAX_UPLOAD_MB=300','JARBAS_AI_MAX_PDF_FILES=10','JARBAS_AI_MAX_PDF_MB=200','JARBAS_AI_TIMEOUT=240','JARBAS_AI_PDF_ALWAYS=1','JARBAS_AI_TETO_USD_MES=50','JARBAS_PAPEL_EXTRACAO=anthropic:claude-sonnet-5','JARBAS_PAPEL_ESTRATEGIA=anthropic:claude-opus-5','JARBAS_PAPEL_REDACAO=anthropic:claude-opus-5','JARBAS_PAPEL_CRITICA=anthropic:claude-sonnet-5','JARBAS_PAPEL_ROTINA=anthropic:claude-haiku-4-5-20251001',"JARBAS_AI_MODEL_LEGAL=$LegalModel","JARBAS_AI_MODEL_INTAKE=$IntakeModel","JARBAS_AI_MODEL_ROUTINE=$RoutineModel","JARBAS_PORT=$Port");if($AnthropicKey){$envLines += "ANTHROPIC_API_KEY=$AnthropicKey"};Say 'Chaves de OpenAI/Gemini, se existiam, nao foram copiadas: revogue-as nos paineis.' DarkGray;[IO.File]::WriteAllLines($EnvFile,$envLines,[Text.UTF8Encoding]::new($false));[IO.File]::WriteAllText((Join-Path $InstallDir 'PORTA_LOCAL.txt'),"$Port`r`n",[Text.ASCIIEncoding]::new())

  Say '[10/19] Definindo credenciais e migrando banco...' Cyan
  $AdminPassword=Read-NewAdminPassword;$DeveloperPassword='Dev83-'+(Token 30);$env:JARBAS_ROOT=$InstallDir;$env:JARBAS_ADMIN_EMAIL=$AdminEmail;$env:JARBAS_BOOTSTRAP_ADMIN_PASSWORD=$AdminPassword;$env:JARBAS_DEVELOPER_EMAIL=$DeveloperEmail;$env:JARBAS_BOOTSTRAP_DEVELOPER_PASSWORD=$DeveloperPassword
  & $Python (Join-Path $InstallDir 'tools\bootstrap_admin.py') *> $BootstrapLog;$bootstrapRc=$LASTEXITCODE
  Remove-Item Env:JARBAS_BOOTSTRAP_DEVELOPER_PASSWORD -ErrorAction SilentlyContinue
  if($bootstrapRc -ne 0){Say 'Falha no bootstrap. Ultimas linhas:' Red;if(Test-Path $BootstrapLog){Get-Content $BootstrapLog -Tail 100 | Write-Host};throw "Falha ao inicializar/migrar banco e credenciais (codigo $bootstrapRc)."}
  $cred=@"
JARBAS Juridico Enterprise 9.0.2

Acesso: http://127.0.0.1:$Port/login
Administrador: $AdminEmail
Senha: a senha definida durante esta instalacao.

Desenvolvedor local: $DeveloperEmail
Senha do desenvolvedor local: $DeveloperPassword

A conta de desenvolvedor e exclusiva desta instalacao local. Nao existe senha mestra global.
"@;[IO.File]::WriteAllText((Join-Path $InstallDir 'CREDENCIAIS_INICIAIS.txt'),$cred,[Text.UTF8Encoding]::new($false));[IO.File]::WriteAllText((Join-Path $InstallDir 'CREDENCIAL_DESENVOLVEDOR.txt'),"$DeveloperEmail`r`n$DeveloperPassword`r`n",[Text.UTF8Encoding]::new($false))

  Say '[11/19] Testando reinicializacao sem senha em variavel...' Cyan
  Remove-Item Env:JARBAS_BOOTSTRAP_ADMIN_PASSWORD -ErrorAction SilentlyContinue;Remove-Item Env:JARBAS_ADMIN_PASSWORD -ErrorAction SilentlyContinue
  & $Python -c "import sys;sys.path.insert(0,r'$InstallDir');from app import main;main.init_db();print('PRODUCTION_RESTART_WITHOUT_PASSWORD_OK')";if($LASTEXITCODE -ne 0){throw 'O banco nao reinicializa em producao sem senha em texto.'}

  Say '[12/19] Executando self-test auditado de banco, Intake, rotas e modulos...' Cyan
  $env:JARBAS_BOOTSTRAP_ADMIN_PASSWORD=$AdminPassword;$selfOut=Join-Path $LogDir 'selftest-9.0.2-out.log';$selfErr=Join-Path $LogDir 'selftest-9.0.2-err.log';Remove-Item $selfOut,$selfErr -Force -ErrorAction SilentlyContinue;$proc=Start-Process -FilePath $Python -ArgumentList @((Join-Path $InstallDir 'tools\validate_install.py')) -WorkingDirectory $InstallDir -Wait -PassThru -NoNewWindow -RedirectStandardOutput $selfOut -RedirectStandardError $selfErr;$selfRc=$proc.ExitCode;Remove-Item Env:JARBAS_BOOTSTRAP_ADMIN_PASSWORD -ErrorAction SilentlyContinue;if($selfRc -ne 0){Say 'Self-test falhou. Saida:' Red;if(Test-Path $selfOut){Get-Content $selfOut -Tail 80|Write-Host};if(Test-Path $selfErr){Get-Content $selfErr -Tail 80|Write-Host};throw 'Self-test integrado falhou. O sistema nao sera considerado instalado.'}else{if(Test-Path $selfOut){Get-Content $selfOut -Tail 40|Write-Host};if((Test-Path $selfErr) -and ((Get-Item $selfErr).Length -gt 0)){Log ('Self-test gerou avisos nao fatais: '+((Get-Content $selfErr -Tail 5)-join ' | '))}}

  Say '[13/19] Reprocessando INTEGRALMENTE PDFs existentes com o pipeline 9.0.2...' Cyan
  $env:JARBAS_ROOT=$InstallDir
  $reindexOut=Join-Path $LogDir 'reindex-pdfs-9.0.2.log';$reindexErr=Join-Path $LogDir 'reindex-pdfs-9.0.2-errors.log'
  Remove-Item $reindexOut,$reindexErr -Force -ErrorAction SilentlyContinue
  $reindexProc=Start-Process -FilePath $Python -ArgumentList @((Join-Path $InstallDir 'tools\reindex_pdfs.py')) -WorkingDirectory $InstallDir -Wait -PassThru -NoNewWindow -RedirectStandardOutput $reindexOut -RedirectStandardError $reindexErr
  $reindexRc=$reindexProc.ExitCode
  if(Test-Path $reindexOut){Get-Content $reindexOut -Tail 160|Write-Host}
  if($reindexRc -ne 0){Say 'Alguns PDFs tiveram falha real de reindexacao. O restante foi preservado; consulte os logs.' Yellow;if(Test-Path $reindexErr){Get-Content $reindexErr -Tail 80|Write-Host};Log "Reindexacao retornou codigo $reindexRc"}else{Say 'PDFs existentes reindexados pelo pipeline 9.0.2.' Green;if((Test-Path $reindexErr) -and ((Get-Item $reindexErr).Length -gt 0)){Log ('Reindexacao gerou avisos nao fatais: '+((Get-Content $reindexErr -Tail 5)-join ' | '))}}

  Say '[14/19] Instalando ferramentas de manutencao e integridade...' Cyan
  foreach($f in Get-ChildItem $ScriptsSource -Filter '*.ps1'){Copy-Item $f.FullName (Join-Path $InstallDir $f.Name) -Force}
  $wrappers=@{'INICIAR_JARBAS.cmd'='INICIAR_JARBAS.ps1';'PARAR_JARBAS.cmd'='PARAR_JARBAS.ps1';'DIAGNOSTICO_JARBAS.cmd'='DIAGNOSTICO_JARBAS.ps1';'CONFIGURAR_OPENAI.cmd'='CONFIGURAR_IA.ps1';'RESETAR_SENHA.cmd'='RESETAR_SENHA.ps1';'BACKUP_JARBAS.cmd'='BACKUP_JARBAS.ps1';'BACKUP_AUTOMATICO.cmd'='BACKUP_AUTOMATICO.ps1';'DESINSTALAR_JARBAS.cmd'='DESINSTALAR_JARBAS.ps1';'VERIFICAR_INTEGRIDADE.cmd'='VERIFICAR_INTEGRIDADE.ps1';'REPROCESSAR_PDFS.cmd'='REPROCESSAR_PDFS.ps1'}
  foreach($kv in $wrappers.GetEnumerator()){[IO.File]::WriteAllText((Join-Path $InstallDir $kv.Key),"@echo off`r`nchcp 65001 >nul`r`npowershell.exe -NoProfile -ExecutionPolicy Bypass -File `"%~dp0$($kv.Value)`"`r`n",[Text.ASCIIEncoding]::new())}
  Write-InstalledManifest

  Say '[15/19] Configuracao facilitada do Claude (Anthropic)...' Cyan
  if($AnthropicKey){Say 'Chave Claude existente foi preservada.' Green}else{
    Say '' ; Say 'Claude e o provedor de IA do JARBAS.' Cyan
    Say 'Gere a chave em console.anthropic.com.' DarkGray
    $want=Read-Host 'Deseja configurar o Claude agora? (S/N)'
    if($want -match '^[Ss]'){
      $newKey=SecretPlain 'Cole a ANTHROPIC_API_KEY'
      if($newKey){
        $env:ANTHROPIC_API_KEY=$newKey;$env:JARBAS_AI_MODEL_LEGAL=$LegalModel;$env:JARBAS_AI_MODEL_INTAKE=$IntakeModel;$env:JARBAS_AI_MODEL_ROUTINE=$RoutineModel
        & $Python (Join-Path $InstallDir 'tools\test_claude.py');$aiRc=$LASTEXITCODE
        Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
        if($aiRc -eq 0){$AnthropicKey=$newKey;$envLines += "ANTHROPIC_API_KEY=$AnthropicKey";[IO.File]::WriteAllLines($EnvFile,$envLines,[Text.UTF8Encoding]::new($false));Say 'Claude validado e salvo.' Green}
        else{Say 'A chave NAO foi salva porque o teste online falhou. O JARBAS segue funcional em modo local. Use CONFIGURAR_IA.cmd depois.' Yellow}
      }
    }
  }
  if($AnthropicKey){Say 'IA ativa: Claude (provedor unico).' Green}else{Say 'Nenhuma IA configurada. O JARBAS roda em modo local.' Yellow}
  # Este bloco vinha da 8.5, quando o Conselho era tri-IA. $OpenAIKey e
  # $GeminiKey NAO EXISTEM mais no instalador 9.0 — sao sempre nulos — de modo
  # que a mensagem impressa era invariavelmente 'Critica adversarial DESLIGADA
  # — configure uma segunda chave em /conselho'. Instrucao sem destino: a 9.0
  # tem a Anthropic como provedor unico e nao aceita chave de outro provedor.
  # Pior, contradizia a linha logo acima, que anuncia 'provedor unico'.
  #
  # O que a 9.0 realmente faz (app/ai_council.py): a critica RODA, entre
  # modelos Claude distintos, e o proprio sistema registra que modelos de
  # mesma linhagem compartilham pontos cegos. E isso que o operador precisa
  # ouvir — porque muda o que ele tem de conferir a mao.
  if($AnthropicKey){
    Say 'Conselho ativo: redacao e critica em modelos Claude distintos.' Green
    Say 'Modelos de mesma linhagem compartilham pontos cegos: confira cada' Yellow
    Say 'fundamento e cada jurisprudencia manualmente antes de protocolar.' Yellow
  }else{
    Say 'Nenhuma IA configurada. O JARBAS roda em modo local.' Yellow
  }

  Say '[16/19] Iniciando servidor e verificando health-check...' Cyan
  & (Join-Path $InstallDir 'INICIAR_JARBAS.ps1');if($LASTEXITCODE -ne 0){throw "Servidor nao iniciou. Codigo $LASTEXITCODE"}
  $portLine=Get-Content $EnvFile|Where-Object{$_ -match '^JARBAS_PORT=(\d+)$'}|Select-Object -First 1;if($portLine){$Port=[int]$portLine.Split('=',2)[1]}

  Say '[17/19] Validando login administrativo real...' Cyan
  $env:JARBAS_PORT="$Port";$env:JARBAS_ADMIN_EMAIL=$AdminEmail;$env:JARBAS_LIVE_TEST_PASSWORD=$AdminPassword;& $Python (Join-Path $InstallDir 'tools\live_login_check.py');$liveRc=$LASTEXITCODE;Remove-Item Env:JARBAS_LIVE_TEST_PASSWORD -ErrorAction SilentlyContinue;if($liveRc -ne 0){throw 'O servidor abriu, mas o login administrativo real nao foi validado.'};Say 'Login administrativo validado no servidor real.' Green

  Say '[18/19] Criando atalhos profissionais...' Cyan
  $desktop=[Environment]::GetFolderPath('Desktop');Get-ChildItem $desktop -Filter 'JARBAS*.lnk' -ErrorAction SilentlyContinue|Remove-Item -Force -ErrorAction SilentlyContinue;$ws=New-Object -ComObject WScript.Shell;$shortcut=$ws.CreateShortcut((Join-Path $desktop 'JARBAS Juridico 9.0.2.lnk'));$shortcut.TargetPath="$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe";$shortcut.Arguments="-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $InstallDir 'INICIAR_JARBAS.ps1')`"";$shortcut.WorkingDirectory=$InstallDir;$shortcut.Description='JARBAS Juridico Enterprise 9.0.2';$shortcut.Save();$startDir=Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\JARBAS Juridico';New-Item -ItemType Directory -Force -Path $startDir|Out-Null;Copy-Item (Join-Path $desktop 'JARBAS Juridico 9.0.2.lnk') (Join-Path $startDir 'JARBAS Juridico 9.0.2.lnk') -Force

  Say '[19/19] Agendando backup diario e registrando instalacao...' Cyan
  # Backup manual e backup que nao acontece: depende de alguem lembrar no dia
  # em que o escritorio esta corrido — que e o dia em que a maquina falha.
  try{
    $agendar=Join-Path $InstallDir 'BACKUP_AUTOMATICO.ps1'
    if(Test-Path $agendar){
      & $agendar -Hora '12:10'
      Say 'Backup diario agendado (12:10). Cancele com BACKUP_AUTOMATICO.ps1 -Remover.' Green
    }
  }catch{
    Say 'Nao foi possivel agendar o backup automatico (requer Administrador).' Yellow
    Say 'Rode BACKUP_AUTOMATICO.ps1 como Administrador depois. Ate la, use BACKUP_JARBAS.cmd.' Yellow
    Log ('Agendamento de backup falhou: '+$_.Exception.Message)
  }

  [IO.File]::WriteAllText((Join-Path $InstallDir 'INSTALACAO_OK_9_0_2.txt'),"JARBAS 9.0.2 instalado em $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')`r`n",[Text.UTF8Encoding]::new($false));Log 'INSTALACAO CONCLUIDA COM SUCESSO';Remove-Item Env:JARBAS_BOOTSTRAP_ADMIN_PASSWORD -ErrorAction SilentlyContinue
  Say '';Say 'JARBAS JURIDICO ENTERPRISE 9.0.2 INSTALADO, TESTADO E INICIADO.' Green;Say "Acesso: http://127.0.0.1:$Port/login" Green;Say "Administrador: $AdminEmail" Green;Say 'A senha do administrador e a senha definida durante a instalacao.' Yellow;Say '' ;Say 'RECOMENDADO para quem opera com autos reais:' Cyan;Say '  1. Ative a verificacao em duas etapas em Configuracoes > Seguranca.' Cyan;Say '     Para exigi-la de todos, ponha JARBAS_2FA_OBRIGATORIO=1 no .env.local.' Cyan;Say '  2. Confira em Documentos\JARBAS_Backups se as copias estao saindo.' Cyan;Say '  3. Teste UMA restauracao por semestre: backup nunca restaurado nao e backup.' Cyan;Say "Credencial de desenvolvedor: $(Join-Path $InstallDir 'CREDENCIAL_DESENVOLVEDOR.txt')" Yellow;Start-Process notepad.exe (Join-Path $InstallDir 'CREDENCIAIS_INICIAIS.txt');exit 0
}catch{
  $msg=$_.Exception.Message;Log "ERRO: $msg";Say '';Say "ERRO NA INSTALACAO: $msg" Red;Say "Log: $InstallLog" Yellow;try{if(Test-Path (Join-Path $InstallDir 'DIAGNOSTICO_JARBAS.ps1')){& (Join-Path $InstallDir 'DIAGNOSTICO_JARBAS.ps1')}}catch{};Say 'O backup dos dados anteriores foi preservado. Nao apague manualmente pastas ou bancos.' Yellow;exit 82
}
