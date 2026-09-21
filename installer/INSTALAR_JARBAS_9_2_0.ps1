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
$InstallLog=Join-Path $LogDir 'instalacao-9.2.0.log'
$BootstrapLog=Join-Path $LogDir 'bootstrap-admin-9.2.0.log'
$AdminEmail='admin@chagasadvogados.local'
$DeveloperEmail='developer@jarbas.local'

function Say([string]$Text,[string]$Color='Gray'){Write-Host $Text -ForegroundColor $Color}

# --------------------------------------------------------------- aparencia
#
# A instalacao anterior era uma parede de texto cinza com 19 linhas iguais.
# Quem instala nao e tecnico: precisa ver ONDE esta, QUANTO falta e se o que
# acabou de acontecer deu certo. Tudo aqui e ASCII de proposito — o console
# do Windows em maquina antiga transforma caractere de moldura em simbolo
# ilegivel, e um instalador que parece quebrado assusta mais do que informa.
$TotalPassos=19
$Larg=72
function Regua([string]$Cor='DarkCyan'){Write-Host ('  +'+('-'*$Larg)+'+') -ForegroundColor $Cor}
function LinhaCaixa([string]$Texto,[string]$Cor='Gray'){
  $t=$Texto;if($t.Length -gt ($Larg-2)){$t=$t.Substring(0,$Larg-5)+'...'}
  Write-Host ('  | '+$t.PadRight($Larg-2)+' |') -ForegroundColor $Cor
}
function Caixa([string[]]$Linhas,[string]$Cor='Cyan'){
  Regua;foreach($l in $Linhas){LinhaCaixa $l $Cor};Regua;Write-Host ''
}
function Passo([int]$N,[string]$Titulo){
  $pct=[int]((($N-1)*100)/$TotalPassos)
  Write-Progress -Activity 'Instalando o JARBAS Juridico' -Status "Passo $N de $TotalPassos - $Titulo" -PercentComplete $pct
  $barra='['+('#'*[int]($pct/5))+('.'*(20-[int]($pct/5)))+"] $pct%"
  Write-Host ''
  Write-Host ("  PASSO $N de $TotalPassos  $barra") -ForegroundColor DarkCyan
  Write-Host ("  $Titulo") -ForegroundColor Cyan
  Log "[$N/$TotalPassos] $Titulo"
}
function OK([string]$Texto){Write-Host ('     [OK] '+$Texto) -ForegroundColor Green}
function Aviso([string]$Texto){Write-Host ('     [!]  '+$Texto) -ForegroundColor Yellow}
function Falha([string]$Texto){Write-Host ('     [X]  '+$Texto) -ForegroundColor Red}
function Nota([string]$Texto){Write-Host ('          '+$Texto) -ForegroundColor DarkGray}
function Pausa([string]$Texto){
  if($env:JARBAS_INSTALL_SEM_PAUSA){return}
  Write-Host '';Write-Host "  $Texto" -ForegroundColor White;[void](Read-Host)
}
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
  $base=Join-Path ([Environment]::GetFolderPath('MyDocuments')) ('JARBAS_Backups\pre-9.2.0-'+(Get-Date -Format 'yyyyMMdd-HHmmss'))
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
Set-Content -Path $InstallLog -Value 'JARBAS 9.2.0 - inicio' -Encoding UTF8
try{
  Clear-Host
  Caixa @(
    'JARBAS JURIDICO ENTERPRISE 9.2.0',
    'CHAGAS - ADVOGADOS',
    '',
    'ERP juridico + Copiloto + Intake de PDF + Financeiro + Claude'
  ) 'Cyan'
  Caixa @(
    'O QUE VAI ACONTECER AGORA',
    '',
    '  1. O pacote e conferido arquivo por arquivo (SHA-256).',
    '  2. Se ja existe um JARBAS nesta maquina, os dados sao copiados',
    '     para Documentos\JARBAS_Backups ANTES de qualquer mudanca.',
    '  3. O Python e as bibliotecas sao instalados numa pasta propria,',
    '     sem mexer em nada que ja esteja no seu Windows.',
    '  4. Voce cria a senha de administrador.',
    '  5. O sistema e testado de verdade (banco, telas e login) e so',
    '     entao e declarado instalado.',
    '',
    'Tempo tipico: 5 a 15 minutos, conforme a internet.',
    'Nenhum dado seu e enviado para fora desta maquina.',
    'A chave da IA e OPCIONAL e pode ser colada depois, pela tela',
    'Configurar IA do proprio sistema.'
  ) 'Gray'
  Pausa 'Pressione ENTER para comecar (ou feche esta janela para cancelar).'
  if(-not [Environment]::Is64BitOperatingSystem){throw 'Esta distribuicao requer Windows 64 bits.'}

  Passo 1 'Conferindo se o pacote chegou inteiro';Verify-Package;OK 'Pacote conferido: todos os arquivos estao integros.'
  Passo 2 'Encerrando o JARBAS, se estiver aberto';$legacyRoots=@(Get-CandidateRoots);Stop-AllJarbas
  Passo 3 'Fazendo copia de seguranca do que ja existe';$BackupRoot=Backup-LegacyRoots $legacyRoots;OK "Copia de seguranca guardada em: $BackupRoot";Nota 'Se algo der errado, seus dados estao la. Nao apague esta pasta.'

  Passo 4 'Instalando o sistema (seus dados nao sao apagados)'
  if(-not(Test-Path $Payload)){throw 'Pasta payload ausente no instalador.'}
  $preserve=Join-Path $env:TEMP ('jarbas-preserve-'+[guid]::NewGuid().ToString('N'));New-Item -ItemType Directory -Force -Path $preserve|Out-Null
  $currentWs=Join-Path $InstallDir 'app\static\workspaces';if(Test-Path $currentWs){Copy-Item $currentWs -Destination (Join-Path $preserve 'workspaces') -Recurse -Force}
  foreach($name in @('app','tools')){$dst=Join-Path $InstallDir $name;if(Test-Path $dst){Remove-Item $dst -Recurse -Force}}
  Copy-Item (Join-Path $Payload 'app') -Destination $InstallDir -Recurse -Force;Copy-Item $ToolsSource -Destination $InstallDir -Recurse -Force;$TestsSource=Join-Path $SourceDir 'tests';if(Test-Path $TestsSource){Copy-Item $TestsSource -Destination $InstallDir -Recurse -Force}
  foreach($f in Get-ChildItem $ScriptsSource -Filter '*.ps1'){Copy-Item $f.FullName (Join-Path $InstallDir $f.Name) -Force}
  $ocrSrc=Join-Path $SourceDir 'ocr';if(Test-Path $ocrSrc){$ocrDst=Join-Path $InstallDir 'installer\ocr';New-Item -ItemType Directory -Force -Path $ocrDst|Out-Null;Copy-Item (Join-Path $ocrSrc '*') $ocrDst -Recurse -Force}
  foreach($name in @('requirements.txt','requirements-ia.txt','requirements-dev.txt','pytest.ini','VERSION.txt','BUILD.txt','LICENSE_PROPRIETARY.txt','README.md','AUDITORIA_8_3.md','AUDITORIA_8_3_1.md','SEGURANCA_LGPD_IA.md','MATRIZ_SISTEMA_PRINCIPAL.md','MATRIZ_FUNCIONAL_7_0.md','ARQUITETURA_SAAS_7_0.md','ROADMAP_PRODUCAO.md','CONSELHO_IA.md','CAPACIDADE_2000_ASSINANTES.md','NOTAS_DA_VERSAO_9_2_0.txt')){$src=Join-Path $Payload $name;if(Test-Path $src){Copy-Item $src -Destination $InstallDir -Force}}
  if(Test-Path (Join-Path $preserve 'workspaces')){Merge-Dir (Join-Path $preserve 'workspaces') (Join-Path $InstallDir 'app\static\workspaces')}

  Passo 5 'Preparando o Python proprio do JARBAS'
  $Python=Join-Path $RuntimeDir 'python.exe'
  if(-not(Test-Path $Python)){
    if(Test-Path $RuntimeDir){Remove-Item $RuntimeDir -Recurse -Force -ErrorAction SilentlyContinue};$runtimeSource=$null
    foreach($r in $legacyRoots){$candidate=Join-Path $r 'runtime\python.exe';if(Test-Path $candidate){$runtimeSource=Join-Path $r 'runtime';break}}
    if($runtimeSource){Say "Reutilizando runtime existente: $runtimeSource" DarkGray;Copy-Item $runtimeSource -Destination $InstallDir -Recurse -Force}else{New-Item -ItemType Directory -Force -Path $RuntimeDir|Out-Null;$zip=Join-Path $DownloadDir 'python-3.12.10-embed-amd64.zip';if(-not(Test-Path $zip)){Download-File 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip' $zip};Expand-Archive $zip $RuntimeDir -Force}
  }
  Repair-Pth $RuntimeDir;& $Python --version;if($LASTEXITCODE -ne 0){throw 'Python portatil nao iniciou.'}

  Passo 6 'Instalando as bibliotecas necessarias'
  $pipOk=$false;try{& $Python -m pip --version *> $null;if($LASTEXITCODE -eq 0){$pipOk=$true}}catch{}
  if(-not $pipOk){$gp=Join-Path $DownloadDir 'get-pip.py';if(-not(Test-Path $gp)){Download-File 'https://bootstrap.pypa.io/get-pip.py' $gp};& $Python $gp --disable-pip-version-check --no-warn-script-location;if($LASTEXITCODE -ne 0){throw 'Falha ao instalar pip no runtime.'}}
  $env:PIP_DISABLE_PIP_VERSION_CHECK='1';$env:PIP_NO_PYTHON_VERSION_WARNING='1'
  # Dependencias embutidas no pacote, quando existirem.
  #
  # Baixar do PyPI na hora da instalacao e o passo que mais falha na maquina
  # do escritorio: proxy corporativo, antivirus que inspeciona HTTPS, internet
  # instavel. Com as rodas dentro do pacote a instalacao nao depende da rede —
  # e, por serem exatamente as versoes fixadas no requirements.txt, instala o
  # mesmo conjunto que foi testado, e nao o que o PyPI servir naquele dia.
  $Rodas=Join-Path $SourceDir 'vendor\wheels'
  $depsOk=$false
  if(Test-Path $Rodas){
    Say 'Instalando dependencias do proprio pacote (sem internet)...' Cyan
    & $Python -m pip install --no-index --find-links "$Rodas" -r (Join-Path $InstallDir 'requirements.txt') --no-warn-script-location
    if($LASTEXITCODE -eq 0){$depsOk=$true;OK 'Bibliotecas instaladas do proprio pacote (sem internet).'}
    else{Aviso 'As bibliotecas do pacote nao serviram; tentando baixar da internet...'}
  }
  if(-not $depsOk){
    & $Python -m pip install -r (Join-Path $InstallDir 'requirements.txt') --prefer-binary --retries 5 --timeout 90 --no-warn-script-location
    if($LASTEXITCODE -ne 0){throw 'Falha ao instalar dependencias. Verifique internet, proxy/antivirus e execute o diagnostico.'}
  }
  # Conselho tri-IA: dependencias OPCIONAIS. Falha aqui NAO aborta a instalacao —
  # o sistema roda sem o provedor correspondente e /conselho informa qual falta.
  $ReqIA=Join-Path $InstallDir 'requirements-ia.txt'
  if(Test-Path $ReqIA){
    Say 'Instalando SDKs do Conselho tri-IA (opcional)...' DarkGray
    if(Test-Path $Rodas){& $Python -m pip install --no-index --find-links "$Rodas" -r $ReqIA --no-warn-script-location}
    if($LASTEXITCODE -ne 0 -or -not(Test-Path $Rodas)){& $Python -m pip install -r $ReqIA --prefer-binary --retries 3 --timeout 90 --no-warn-script-location}
    if($LASTEXITCODE -ne 0){
      Say 'AVISO: os SDKs de Anthropic/Google nao foram instalados.' Yellow
      Say 'O JARBAS funciona normalmente; o Conselho ficara limitado aos provedores disponiveis.' Yellow
      Say 'Para tentar de novo depois: pip install -r requirements-ia.txt' DarkGray
      $global:LASTEXITCODE=0
    } else { Say 'SDKs do Conselho tri-IA instalados.' Green }
  }

  Passo 7 'Procurando o banco com os seus dados'
  $dbCandidates=New-Object System.Collections.Generic.List[string];foreach($r in @(Get-CandidateRoots)){$d=Join-Path $r 'data\jarbas.db';if(Test-Path $d){$dbCandidates.Add($d)}}
  $BestDb='';if($dbCandidates.Count -gt 0){$scoreOut=& $Python (Join-Path $InstallDir 'tools\score_databases.py') @($dbCandidates);$scoreOut|ForEach-Object{Log $_};$bestLine=$scoreOut|Where-Object{$_ -like 'BEST=*'}|Select-Object -Last 1;if($bestLine){$BestDb=$bestLine.Substring(5)}}
  $SelectedRoot='';if($BestDb){$SelectedRoot=Split-Path (Split-Path $BestDb -Parent) -Parent;OK "Banco encontrado: $BestDb"}else{Nota 'Nenhum banco anterior: sera criado um banco novo e vazio.'}

  Passo 8 'Consolidando banco, PDFs e identidade visual'
  if($SelectedRoot){
    try{$selResolved=(Resolve-Path $SelectedRoot).Path}catch{$selResolved=$SelectedRoot};try{$insResolved=(Resolve-Path $InstallDir).Path}catch{$insResolved=$InstallDir}
    if($selResolved -ne $insResolved){if(Test-Path $DataDir){Remove-Item $DataDir -Recurse -Force};New-Item -ItemType Directory -Force -Path $DataDir|Out-Null;Merge-Dir (Join-Path $SelectedRoot 'data') $DataDir;Copy-CurrentBranding $SelectedRoot;$canonicalDb=Join-Path $DataDir 'jarbas.db';if(Test-Path $BestDb){Copy-Item $BestDb $canonicalDb -Force};& $Python (Join-Path $InstallDir 'tools\rebase_paths.py') $canonicalDb $SelectedRoot $InstallDir|Out-Null}
  }

  Passo 9 'Gravando a configuracao desta instalacao'
  $OldEnv='';if(Test-Path (Join-Path $InstallDir '.env.local')){$OldEnv=Join-Path $InstallDir '.env.local'}elseif($SelectedRoot -and (Test-Path (Join-Path $SelectedRoot '.env.local'))){$OldEnv=Join-Path $SelectedRoot '.env.local'}
  $AnthropicKey=EnvValue $OldEnv 'ANTHROPIC_API_KEY';$LegalModel=EnvValue $OldEnv 'JARBAS_AI_MODEL_LEGAL';$IntakeModel=EnvValue $OldEnv 'JARBAS_AI_MODEL_INTAKE';$RoutineModel=EnvValue $OldEnv 'JARBAS_AI_MODEL_ROUTINE';# Migracao da 8.x: o .env.local antigo traz modelos da OpenAI. Preservar
  # esses nomes faz o gateway enviar 'gpt-5.6-sol' para a Anthropic.
  if($LegalModel -notlike 'claude-*'){$LegalModel='claude-opus-5'}
  if($IntakeModel -notlike 'claude-*'){$IntakeModel='claude-sonnet-5'}
  if($RoutineModel -notlike 'claude-*'){$RoutineModel='claude-haiku-4-5-20251001'}
  $Port=Find-FreePort;$Secret=Token 72;$EnvFile=Join-Path $InstallDir '.env.local';$envLines=@('JARBAS_ENV=production',"JARBAS_SECRET_KEY=$Secret","JARBAS_ADMIN_EMAIL=$AdminEmail",'JARBAS_ALLOWED_HOSTS=localhost,127.0.0.1','JARBAS_HTTPS_ONLY=0','JARBAS_PUBLIC_SIGNUP=0','JARBAS_2FA_OBRIGATORIO=0','JARBAS_SESSION_MAX_AGE=28800','JARBAS_TRUSTED_PROXY_HOPS=0','JARBAS_ALLOW_INDEXING=0','JARBAS_ALLOW_SECRET_CONFIG=1','JARBAS_PERMITE_MAPEAR_PASTA=1','JARBAS_IMPORT_MAX_MB=2000','JARBAS_IMPORT_MAX_IA=25','JARBAS_CHATBOT_TETO_USD_MES=15','JARBAS_MAX_UPLOAD_MB=300','JARBAS_AI_MAX_PDF_FILES=10','JARBAS_AI_MAX_PDF_MB=200','JARBAS_AI_TIMEOUT=240','JARBAS_AI_PDF_ALWAYS=1','JARBAS_AI_TETO_USD_MES=50','JARBAS_PAPEL_EXTRACAO=anthropic:claude-sonnet-5','JARBAS_PAPEL_ESTRATEGIA=anthropic:claude-opus-5','JARBAS_PAPEL_REDACAO=anthropic:claude-opus-5','JARBAS_PAPEL_CRITICA=anthropic:claude-sonnet-5','JARBAS_PAPEL_ROTINA=anthropic:claude-haiku-4-5-20251001',"JARBAS_AI_MODEL_LEGAL=$LegalModel","JARBAS_AI_MODEL_INTAKE=$IntakeModel","JARBAS_AI_MODEL_ROUTINE=$RoutineModel","JARBAS_PORT=$Port");if($AnthropicKey){$envLines += "ANTHROPIC_API_KEY=$AnthropicKey"};Say 'Chaves de OpenAI/Gemini, se existiam, nao foram copiadas: revogue-as nos paineis.' DarkGray;[IO.File]::WriteAllLines($EnvFile,$envLines,[Text.UTF8Encoding]::new($false));[IO.File]::WriteAllText((Join-Path $InstallDir 'PORTA_LOCAL.txt'),"$Port`r`n",[Text.ASCIIEncoding]::new())

  Passo 10 'Criando a sua senha de administrador'
  $AdminPassword=Read-NewAdminPassword;$DeveloperPassword='Dev83-'+(Token 30);$env:JARBAS_ROOT=$InstallDir;$env:JARBAS_ADMIN_EMAIL=$AdminEmail;$env:JARBAS_BOOTSTRAP_ADMIN_PASSWORD=$AdminPassword;$env:JARBAS_DEVELOPER_EMAIL=$DeveloperEmail;$env:JARBAS_BOOTSTRAP_DEVELOPER_PASSWORD=$DeveloperPassword
  & $Python (Join-Path $InstallDir 'tools\bootstrap_admin.py') *> $BootstrapLog;$bootstrapRc=$LASTEXITCODE
  Remove-Item Env:JARBAS_BOOTSTRAP_DEVELOPER_PASSWORD -ErrorAction SilentlyContinue
  if($bootstrapRc -ne 0){Say 'Falha no bootstrap. Ultimas linhas:' Red;if(Test-Path $BootstrapLog){Get-Content $BootstrapLog -Tail 100 | Write-Host};throw "Falha ao inicializar/migrar banco e credenciais (codigo $bootstrapRc)."}
  $cred=@"
JARBAS Juridico Enterprise 9.2.0

Acesso: http://127.0.0.1:$Port/login
Administrador: $AdminEmail
Senha: a senha definida durante esta instalacao.

Desenvolvedor local: $DeveloperEmail
Senha do desenvolvedor local: $DeveloperPassword

A conta de desenvolvedor e exclusiva desta instalacao local. Nao existe senha mestra global.
"@;[IO.File]::WriteAllText((Join-Path $InstallDir 'CREDENCIAIS_INICIAIS.txt'),$cred,[Text.UTF8Encoding]::new($false));[IO.File]::WriteAllText((Join-Path $InstallDir 'CREDENCIAL_DESENVOLVEDOR.txt'),"$DeveloperEmail`r`n$DeveloperPassword`r`n",[Text.UTF8Encoding]::new($false))

  Passo 11 'Conferindo que o sistema reabre sozinho'
  Remove-Item Env:JARBAS_BOOTSTRAP_ADMIN_PASSWORD -ErrorAction SilentlyContinue;Remove-Item Env:JARBAS_ADMIN_PASSWORD -ErrorAction SilentlyContinue
  & $Python -c "import sys;sys.path.insert(0,r'$InstallDir');from app import main;main.init_db();print('PRODUCTION_RESTART_WITHOUT_PASSWORD_OK')";if($LASTEXITCODE -ne 0){throw 'O banco nao reinicializa em producao sem senha em texto.'}

  Passo 12 'Testando banco, telas e modulos'
  $env:JARBAS_BOOTSTRAP_ADMIN_PASSWORD=$AdminPassword;$selfOut=Join-Path $LogDir 'selftest-9.2.0-out.log';$selfErr=Join-Path $LogDir 'selftest-9.2.0-err.log';Remove-Item $selfOut,$selfErr -Force -ErrorAction SilentlyContinue;$proc=Start-Process -FilePath $Python -ArgumentList @((Join-Path $InstallDir 'tools\validate_install.py')) -WorkingDirectory $InstallDir -Wait -PassThru -NoNewWindow -RedirectStandardOutput $selfOut -RedirectStandardError $selfErr;$selfRc=$proc.ExitCode;Remove-Item Env:JARBAS_BOOTSTRAP_ADMIN_PASSWORD -ErrorAction SilentlyContinue;if($selfRc -ne 0){Say 'Self-test falhou. Saida:' Red;if(Test-Path $selfOut){Get-Content $selfOut -Tail 80|Write-Host};if(Test-Path $selfErr){Get-Content $selfErr -Tail 80|Write-Host};throw 'Self-test integrado falhou. O sistema nao sera considerado instalado.'}else{if(Test-Path $selfOut){Get-Content $selfOut -Tail 40|Write-Host};if((Test-Path $selfErr) -and ((Get-Item $selfErr).Length -gt 0)){Log ('Self-test gerou avisos nao fatais: '+((Get-Content $selfErr -Tail 5)-join ' | '))}}

  Passo 13 'Relendo os PDFs ja cadastrados'
  $env:JARBAS_ROOT=$InstallDir
  $reindexOut=Join-Path $LogDir 'reindex-pdfs-9.2.0.log';$reindexErr=Join-Path $LogDir 'reindex-pdfs-9.2.0-errors.log'
  Remove-Item $reindexOut,$reindexErr -Force -ErrorAction SilentlyContinue
  $reindexProc=Start-Process -FilePath $Python -ArgumentList @((Join-Path $InstallDir 'tools\reindex_pdfs.py')) -WorkingDirectory $InstallDir -Wait -PassThru -NoNewWindow -RedirectStandardOutput $reindexOut -RedirectStandardError $reindexErr
  $reindexRc=$reindexProc.ExitCode
  if(Test-Path $reindexOut){Get-Content $reindexOut -Tail 160|Write-Host}
  if($reindexRc -ne 0){Say 'Alguns PDFs tiveram falha real de reindexacao. O restante foi preservado; consulte os logs.' Yellow;if(Test-Path $reindexErr){Get-Content $reindexErr -Tail 80|Write-Host};Log "Reindexacao retornou codigo $reindexRc"}else{OK 'PDFs ja cadastrados foram relidos pelo pipeline 9.2.0.';if((Test-Path $reindexErr) -and ((Get-Item $reindexErr).Length -gt 0)){Log ('Reindexacao gerou avisos nao fatais: '+((Get-Content $reindexErr -Tail 5)-join ' | '))}}

  Passo 14 'Instalando os atalhos de manutencao'
  foreach($f in Get-ChildItem $ScriptsSource -Filter '*.ps1'){Copy-Item $f.FullName (Join-Path $InstallDir $f.Name) -Force}
  $wrappers=@{'INICIAR_JARBAS.cmd'='INICIAR_JARBAS.ps1';'PARAR_JARBAS.cmd'='PARAR_JARBAS.ps1';'DIAGNOSTICO_JARBAS.cmd'='DIAGNOSTICO_JARBAS.ps1';'CONFIGURAR_OPENAI.cmd'='CONFIGURAR_IA.ps1';'CONFIGURAR_IA.cmd'='CONFIGURAR_IA.ps1';'RESETAR_SENHA.cmd'='RESETAR_SENHA.ps1';'BACKUP_JARBAS.cmd'='BACKUP_JARBAS.ps1';'BACKUP_AUTOMATICO.cmd'='BACKUP_AUTOMATICO.ps1';'INSTALAR_OCR.cmd'='INSTALAR_OCR.ps1';'DESINSTALAR_JARBAS.cmd'='DESINSTALAR_JARBAS.ps1';'VERIFICAR_INTEGRIDADE.cmd'='VERIFICAR_INTEGRIDADE.ps1';'REPROCESSAR_PDFS.cmd'='REPROCESSAR_PDFS.ps1'}
  foreach($kv in $wrappers.GetEnumerator()){[IO.File]::WriteAllText((Join-Path $InstallDir $kv.Key),"@echo off`r`nchcp 65001 >nul`r`npowershell.exe -NoProfile -ExecutionPolicy Bypass -File `"%~dp0$($kv.Value)`"`r`n",[Text.ASCIIEncoding]::new())}
  Write-InstalledManifest

  Passo 15 'Chave da IA (opcional, pode ficar para depois)'
  if($AnthropicKey){OK 'A chave da Claude que ja existia foi preservada.'}else{
    # A chave e OPCIONAL, e dizer isso com clareza importa: na 9.0 o operador
    # pulava este passo por nao ter a chave em maos e ficava sem saber que
    # havia caminho de volta. A partir da 9.2 existe a tela Configurar IA,
    # e este texto aponta para ela.
    Caixa @(
      'CHAVE DA IA - PASSO OPCIONAL',
      '',
      'O JARBAS ja funciona sem chave nenhuma: clientes, processos,',
      'prazos, agenda, financeiro, documentos e busca nos autos.',
      '',
      'A chave liga o Intake de PDF por IA, o Copiloto do processo,',
      'a Central IA e o Conselho. Ela e paga por uso, direto na',
      'Anthropic, e voce a gera em console.anthropic.com > API Keys.',
      '',
      'Se voce ainda nao tem a chave, responda N e siga em frente.',
      'Depois, dentro do sistema, abra CONFIGURAR IA no menu lateral',
      'e cole a chave la. Nada precisa ser reinstalado.'
    ) 'Gray'
    $want=Read-Host '  Quer colar a chave agora? (S/N)'
    if($want -match '^[Ss]'){
      $newKey=SecretPlain '  Cole a chave (ela nao aparece na tela)'
      if($newKey){
        # Saneamento igual ao do sistema: colar a linha inteira do arquivo,
        # com aspas ou com invisivel do navegador, produzia um 401 que nao
        # tinha como ser diagnosticado na tela.
        $newKey=($newKey -replace [string][char]0xFEFF,'' -replace [string][char]0x200B,'').Trim()
        $newKey=($newKey -replace '^\s*ANTHROPIC_API_KEY\s*=','').Trim().Trim('"').Trim("'")
        $env:ANTHROPIC_API_KEY=$newKey;$env:JARBAS_AI_MODEL_LEGAL=$LegalModel;$env:JARBAS_AI_MODEL_INTAKE=$IntakeModel;$env:JARBAS_AI_MODEL_ROUTINE=$RoutineModel
        & $Python (Join-Path $InstallDir 'tools\test_claude.py');$aiRc=$LASTEXITCODE
        Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
        if($aiRc -eq 0){$AnthropicKey=$newKey;$envLines += "ANTHROPIC_API_KEY=$AnthropicKey";[IO.File]::WriteAllLines($EnvFile,$envLines,[Text.UTF8Encoding]::new($false));OK 'Chave testada e salva. A IA esta ligada.'}
        else{Aviso 'A chave NAO foi salva: o teste online falhou.';Nota 'O JARBAS segue funcionando. Abra Configurar IA no menu do sistema';Nota 'e tente de novo — la a mensagem de erro explica o motivo.'}
      }
    }else{Nota 'Sem problema. Abra CONFIGURAR IA no menu do sistema quando tiver a chave.'}
  }
  if($AnthropicKey){OK 'IA ativa: Claude.'}else{Aviso 'Sem IA por enquanto. Todo o resto do sistema esta disponivel.'}
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

  Passo 16 'Ligando o servidor e conferindo a saude'
  & (Join-Path $InstallDir 'INICIAR_JARBAS.ps1');if($LASTEXITCODE -ne 0){throw "Servidor nao iniciou. Codigo $LASTEXITCODE"}
  $portLine=Get-Content $EnvFile|Where-Object{$_ -match '^JARBAS_PORT=(\d+)$'}|Select-Object -First 1;if($portLine){$Port=[int]$portLine.Split('=',2)[1]}

  Passo 17 'Validando um login administrativo real'
  $env:JARBAS_PORT="$Port";$env:JARBAS_ADMIN_EMAIL=$AdminEmail;$env:JARBAS_LIVE_TEST_PASSWORD=$AdminPassword;& $Python (Join-Path $InstallDir 'tools\live_login_check.py');$liveRc=$LASTEXITCODE;Remove-Item Env:JARBAS_LIVE_TEST_PASSWORD -ErrorAction SilentlyContinue;if($liveRc -ne 0){throw 'O servidor abriu, mas o login administrativo real nao foi validado.'};OK 'Login administrativo validado no servidor de verdade.'

  Passo 18 'Criando os atalhos na area de trabalho'
  $desktop=[Environment]::GetFolderPath('Desktop');Get-ChildItem $desktop -Filter 'JARBAS*.lnk' -ErrorAction SilentlyContinue|Remove-Item -Force -ErrorAction SilentlyContinue;$ws=New-Object -ComObject WScript.Shell;$shortcut=$ws.CreateShortcut((Join-Path $desktop 'JARBAS Juridico 9.2.0.lnk'));$shortcut.TargetPath="$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe";$shortcut.Arguments="-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $InstallDir 'INICIAR_JARBAS.ps1')`"";$shortcut.WorkingDirectory=$InstallDir;$shortcut.Description='JARBAS Juridico Enterprise 9.2.0';$shortcut.Save();$startDir=Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\JARBAS Juridico';New-Item -ItemType Directory -Force -Path $startDir|Out-Null;Copy-Item (Join-Path $desktop 'JARBAS Juridico 9.2.0.lnk') (Join-Path $startDir 'JARBAS Juridico 9.2.0.lnk') -Force

  Passo 19 'OCR, backup automatico e registro final'
  # OCR em etapa TOLERANTE A FALHA, como o SDK de IA: um escritorio nao pode
  # ficar sem sistema de processos porque o antivirus bloqueou um download.
  # Sem OCR o JARBAS le normalmente todo PDF com camada de texto; so os autos
  # DIGITALIZADOS ficam sem leitura local.
  try{
    $ocrScript=Join-Path $InstallDir 'INSTALAR_OCR.ps1'
    if(Test-Path $ocrScript){
      & $ocrScript
      if($LASTEXITCODE -eq 0){OK 'OCR instalado, com o idioma portugues.'}
      else{Aviso 'OCR incompleto. Rode INSTALAR_OCR.cmd depois; o resto ja esta pronto.'}
    }
  }catch{
    Say 'Nao foi possivel instalar o OCR agora. Rode INSTALAR_OCR.cmd depois.' Yellow
    Log ('Instalacao do OCR falhou: '+$_.Exception.Message)
  }

  # Backup manual e backup que nao acontece: depende de alguem lembrar no dia
  # em que o escritorio esta corrido — que e o dia em que a maquina falha.
  try{
    $agendar=Join-Path $InstallDir 'BACKUP_AUTOMATICO.ps1'
    if(Test-Path $agendar){
      & $agendar -Hora '12:10'
      OK 'Backup diario agendado para as 12:10.';Nota 'Para cancelar: BACKUP_AUTOMATICO.ps1 -Remover'
    }
  }catch{
    Say 'Nao foi possivel agendar o backup automatico (requer Administrador).' Yellow
    Say 'Rode BACKUP_AUTOMATICO.ps1 como Administrador depois. Ate la, use BACKUP_JARBAS.cmd.' Yellow
    Log ('Agendamento de backup falhou: '+$_.Exception.Message)
  }

  [IO.File]::WriteAllText((Join-Path $InstallDir 'INSTALACAO_OK_9_2_0.txt'),"JARBAS 9.2.0 instalado em $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')`r`n",[Text.UTF8Encoding]::new($false));Log 'INSTALACAO CONCLUIDA COM SUCESSO';Remove-Item Env:JARBAS_BOOTSTRAP_ADMIN_PASSWORD -ErrorAction SilentlyContinue
  Write-Progress -Activity 'Instalando o JARBAS Juridico' -Completed
  Write-Host ''
  Caixa @(
    'INSTALACAO CONCLUIDA, TESTADA E EM FUNCIONAMENTO',
    '',
    "Endereco .......: http://127.0.0.1:$Port/login",
    "Usuario ........: $AdminEmail",
    'Senha ..........: a que voce criou agora ha pouco',
    '',
    'Atalho na area de trabalho: JARBAS Juridico 9.2.0'
  ) 'Green'
  Caixa @(
    'PRIMEIROS PASSOS DENTRO DO SISTEMA',
    '',
    ('  1. ' + $(if($AnthropicKey){'IA ja configurada. Nada a fazer aqui.'}else{'Menu CONFIGURAR IA: cole a chave quando tiver uma.'})),
    '  2. Menu IMPORTAR PASTAS: traga as pastas de clientes que ja',
    '     existem no computador. O sistema le, propoe o cadastro e',
    '     so grava o que voce confirmar.',
    '  3. Configuracoes > Verificacao em duas etapas: ligue agora.',
    '     Com autos sob sigilo, uma senha vazada nao pode bastar.'
  ) 'Cyan'
  Caixa @(
    'PARA QUEM OPERA COM AUTOS REAIS',
    '',
    '  - Confira em Documentos\JARBAS_Backups se as copias estao saindo.',
    '  - Teste UMA restauracao por semestre. Backup nunca restaurado',
    '    nao e backup: e esperanca.',
    '  - Para exigir 2 etapas de todos, ponha JARBAS_2FA_OBRIGATORIO=1',
    '    no arquivo .env.local da pasta de instalacao.',
    '  - Nunca envie a chave da IA por WhatsApp, e-mail ou print.'
  ) 'Yellow'
  Say "Credencial de desenvolvedor: $(Join-Path $InstallDir 'CREDENCIAL_DESENVOLVEDOR.txt')" DarkGray
  Start-Process notepad.exe (Join-Path $InstallDir 'CREDENCIAIS_INICIAIS.txt')
  try{Start-Process "http://127.0.0.1:$Port/login"}catch{}
  exit 0
}catch{
  $msg=$_.Exception.Message;Log "ERRO: $msg"
  try{Write-Progress -Activity 'Instalando o JARBAS Juridico' -Completed}catch{}
  Write-Host ''
  try{Caixa @('A INSTALACAO PAROU','',$msg,'','O que fazer:','  1. Feche o antivirus/VPN e rode INSTALAR.cmd de novo.','  2. Se repetir, envie o log abaixo para o suporte.',"  Log: $InstallLog") 'Red'}catch{Say "ERRO NA INSTALACAO: $msg" Red}
  try{if(Test-Path (Join-Path $InstallDir 'DIAGNOSTICO_JARBAS.ps1')){& (Join-Path $InstallDir 'DIAGNOSTICO_JARBAS.ps1')}}catch{};Say 'O backup dos dados anteriores foi preservado. Nao apague manualmente pastas ou bancos.' Yellow;exit 82
}
