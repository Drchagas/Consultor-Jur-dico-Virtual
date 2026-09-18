$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new() } catch {}

# Instala o OCR (Tesseract) para ler autos DIGITALIZADOS.
#
# Um PDF do eproc gerado pelo sistema tem camada de texto e e lido sem OCR.
# Um auto escaneado - peticao assinada a mao, documento antigo, oficio de
# outro orgao - e so imagem: sem OCR, o JARBAS nao tem uma letra para ler, e
# a unica alternativa e mandar cada pagina para a IA, o que custa dinheiro
# por pagina e depende de internet.
#
# Instalacao em %LOCALAPPDATA%\Programs\Tesseract-OCR: NAO exige privilegio
# de administrador, que e o que costuma travar a instalacao na maquina do
# escritorio.
#
#   .\INSTALAR_OCR.ps1              # instala
#   .\INSTALAR_OCR.ps1 -Verificar   # so confere o que ja existe
param([switch]$Verificar)

$Root=Join-Path $env:LOCALAPPDATA 'JARBAS_Enterprise'
$EnvFile=Join-Path $Root '.env.local'
$Python=Join-Path $Root 'runtime\python.exe'
$Destino=Join-Path $env:LOCALAPPDATA 'Programs\Tesseract-OCR'
$PorEmbutido=Join-Path $Root 'installer\ocr\por.traineddata'

# Versao e origem do instalador do Tesseract para Windows. O build da UB
# Mannheim e o oficialmente indicado pelo projeto Tesseract para Windows.
$Versao='5.5.0.20241111'
$Origens=@(
  "https://digi.bib.uni-mannheim.de/tesseract/tesseract-ocr-w64-setup-$Versao.exe",
  "https://github.com/UB-Mannheim/tesseract/releases/download/v$Versao/tesseract-ocr-w64-setup-$Versao.exe"
)

function Diga([string]$t,[string]$cor='Gray'){Write-Host $t -ForegroundColor $cor}

function Achar-Tesseract {
  $candidatos=@(
    (Join-Path $Destino 'tesseract.exe'),
    (Join-Path $Root 'runtime\tesseract\tesseract.exe'),
    (Join-Path $env:ProgramFiles 'Tesseract-OCR\tesseract.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Tesseract-OCR\tesseract.exe')
  )
  foreach($c in $candidatos){ if($c -and (Test-Path $c)){ return $c } }
  $noPath=Get-Command tesseract -ErrorAction SilentlyContinue
  if($noPath){ return $noPath.Source }
  return ''
}

function Set-EnvValue([string]$Key,[string]$Value){
  $lines=@(); if(Test-Path $EnvFile){$lines=Get-Content $EnvFile}
  $found=$false; $out=New-Object System.Collections.Generic.List[string]
  foreach($line in $lines){
    if($line -match ('^\s*'+[regex]::Escape($Key)+'\s*=')){$out.Add("$Key=$Value");$found=$true}
    else{$out.Add($line)}
  }
  if(-not $found){$out.Add("$Key=$Value")}
  [IO.File]::WriteAllLines($EnvFile,$out,[Text.UTF8Encoding]::new($false))
}

function Instalar-Portugues([string]$Exe){
  # O instalador do Tesseract marca apenas o ingles por padrao. Sem o
  # portugues, o OCR de um auto brasileiro roda e devolve letra embaralhada -
  # um sintoma que nao aponta para a causa.
  $tessdata=Join-Path (Split-Path $Exe -Parent) 'tessdata'
  if(-not(Test-Path $tessdata)){ New-Item -ItemType Directory -Force -Path $tessdata|Out-Null }
  $alvo=Join-Path $tessdata 'por.traineddata'
  if(Test-Path $alvo){ Diga '  portugues ja instalado.' Green; return $true }
  if(Test-Path $PorEmbutido){
    try{
      Copy-Item $PorEmbutido $alvo -Force
      Diga '  portugues instalado a partir do pacote do JARBAS.' Green
      return $true
    }catch{
      Diga "  nao consegui escrever em $tessdata ($($_.Exception.Message))." Yellow
      Diga '  Rode este script como Administrador, ou instale o Tesseract em' Yellow
      Diga '  %LOCALAPPDATA%\Programs\Tesseract-OCR, que dispensa privilegio.' Yellow
      return $false
    }
  }
  Diga '  pacote de portugues nao encontrado no JARBAS; tentando baixar...' DarkGray
  try{
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 `
      'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/por.traineddata' `
      -OutFile $alvo
    Diga '  portugues baixado.' Green; return $true
  }catch{
    Diga '  falhou baixar o portugues. O OCR ficara so em ingles.' Red; return $false
  }
}

function Conferir([string]$Exe){
  Diga ''
  Diga 'Conferindo o OCR...' Cyan
  $langs = & $Exe --list-langs 2>&1 | Out-String
  $temPor = $langs -match '(?m)^\s*por\s*$'
  $versao = (& $Exe --version 2>&1 | Select-Object -First 1)
  Diga "  binario .: $Exe"
  Diga "  versao ..: $versao"
  Diga ("  portugues: " + $(if($temPor){'SIM'}else{'NAO'})) $(if($temPor){'Green'}else{'Red'})
  if(-not $temPor){
    Diga '  Sem o portugues, o OCR de auto brasileiro devolve texto inutilizavel.' Red
    return $false
  }
  return $true
}

# ----------------------------------------------------------------- execucao
Diga '============================================================' Cyan
Diga ' JARBAS - OCR PARA AUTOS DIGITALIZADOS' Cyan
Diga '============================================================' Cyan

$Exe=Achar-Tesseract

if($Verificar){
  if(-not $Exe){ Diga 'Tesseract NAO instalado. Rode INSTALAR_OCR.cmd.' Red; exit 1 }
  if(Conferir $Exe){ exit 0 } else { exit 2 }
}

if($Exe){
  Diga "Tesseract ja instalado em: $Exe" Green
}else{
  Diga "Baixando o Tesseract $Versao..." Cyan
  $temp=Join-Path $env:TEMP "tesseract-$Versao.exe"
  $baixou=$false
  foreach($url in $Origens){
    try{
      Diga "  tentando $url" DarkGray
      Invoke-WebRequest -UseBasicParsing -TimeoutSec 600 $url -OutFile $temp
      $baixou=$true; break
    }catch{
      Diga "  falhou: $($_.Exception.Message)" DarkGray
    }
  }
  if(-not $baixou){
    Diga '' ; Diga 'NAO consegui baixar o Tesseract.' Red
    Diga 'Provavel bloqueio do antivirus ou do proxy do escritorio.' Yellow
    Diga 'Alternativa manual:' Yellow
    Diga '  1. Baixe em https://github.com/UB-Mannheim/tesseract/wiki' Yellow
    Diga '  2. Instale marcando o idioma Portuguese' Yellow
    Diga '  3. Rode este script de novo: ele so completa o que faltar' Yellow
    Diga '' ; Diga 'O JARBAS continua funcionando sem OCR: PDFs com texto sao lidos' Cyan
    Diga 'normalmente. So os autos DIGITALIZADOS ficam sem leitura local.' Cyan
    exit 3
  }

  # /S = silencioso (NSIS). /D= precisa ser o ULTIMO argumento e sem aspas.
  Diga 'Instalando (sem exigir administrador)...' Cyan
  $proc=Start-Process -FilePath $temp -ArgumentList "/S","/D=$Destino" -Wait -PassThru
  Remove-Item $temp -Force -ErrorAction SilentlyContinue
  if($proc.ExitCode -ne 0){ Diga "O instalador do Tesseract retornou $($proc.ExitCode)." Yellow }

  $Exe=Achar-Tesseract
  if(-not $Exe){ Diga 'Instalacao concluida mas o binario nao foi encontrado.' Red; exit 4 }
  Diga "Instalado em: $Exe" Green
}

Diga ''
Diga 'Garantindo o idioma portugues...' Cyan
Instalar-Portugues $Exe | Out-Null

# Registra o caminho: o PATH do Windows so e relido quando a sessao reinicia,
# e o JARBAS precisa achar o binario ainda nesta execucao.
if(Test-Path $EnvFile){
  Set-EnvValue 'JARBAS_TESSERACT' $Exe
  Diga "JARBAS_TESSERACT registrado no .env.local." Green
}

$ok = Conferir $Exe

Diga ''
if($ok){
  Diga 'OCR pronto. Reinicie o JARBAS para ele passar a usar.' Green
  Diga 'Em um processo ja cadastrado, use Copiloto > Reprocessar todos os PDFs' Cyan
  Diga 'para reler os autos digitalizados que antes ficaram sem texto.' Cyan
  exit 0
}
Diga 'OCR instalado, mas sem o portugues. Veja as mensagens acima.' Yellow
exit 2
