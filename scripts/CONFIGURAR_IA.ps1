$ErrorActionPreference='Stop'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new() } catch {}
$Root=Join-Path $env:LOCALAPPDATA 'JARBAS_Enterprise';$EnvFile=Join-Path $Root '.env.local';$Python=Join-Path $Root 'runtime\python.exe'
function SecretPlain([string]$Prompt){$s=Read-Host $Prompt -AsSecureString;$p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s);try{return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($p)}finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p)}}
function Get-Env([string]$Key){if(-not(Test-Path $EnvFile)){return ''};$line=Get-Content $EnvFile|Where-Object{$_ -match ('^\s*'+[regex]::Escape($Key)+'\s*=')}|Select-Object -First 1;if($line){return (($line.Split('=',2)[1]).Trim().Trim('"').Trim("'"))};return ''}
function Set-Env([string]$Key,[string]$Value){$lines=@();if(Test-Path $EnvFile){$lines=Get-Content $EnvFile};$found=$false;$out=New-Object System.Collections.Generic.List[string];foreach($line in $lines){if($line -match ('^\s*'+[regex]::Escape($Key)+'\s*=')){$out.Add("$Key=$Value");$found=$true}else{$out.Add($line)}};if(-not $found){$out.Add("$Key=$Value")};[IO.File]::WriteAllLines($EnvFile,$out,[Text.UTF8Encoding]::new($false))}
if(-not(Test-Path $Root)){throw 'JARBAS nao instalado.'};if(-not(Test-Path $Python)){throw 'Runtime Python nao encontrado.'}
Write-Host '============================================================' -ForegroundColor Cyan;Write-Host ' JARBAS 9.0.2 - CONEXAO COM O CLAUDE (ANTHROPIC)' -ForegroundColor Cyan;Write-Host '============================================================' -ForegroundColor Cyan
$Existing=Get-Env 'ANTHROPIC_API_KEY';if($Existing){Write-Host ('Chave atual configurada: ****'+$Existing.Substring([Math]::Max(0,$Existing.Length-4))) -ForegroundColor Green}
$Key=SecretPlain 'Cole uma NOVA Claude API Key (Enter testa/mantem a atual)';if(-not $Key){$Key=$Existing};if(-not $Key){throw 'Nenhuma chave Claude foi informada.'}
# Colar da pagina do console traz aspas, espaco invisivel ou ate a linha
# inteira do .env.local. Tudo isso segue para a API e volta como 401, com uma
# mensagem que fala em formato de chave e nao em texto colado errado.
$Key=$Key.Trim()
foreach($c in @([char]0xFEFF,[char]0x200B,[char]0x200C,[char]0x200D,[char]0x2060,[char]0x00A0)){$Key=$Key.Replace([string]$c,'')}
if($Key -match '^\s*ANTHROPIC_API_KEY\s*='){$Key=($Key -split '=',2)[1]}
$Key=$Key.Trim().Trim('"').Trim("'").Trim()
if($Key -notlike 'sk-ant-*'){Write-Host 'AVISO: uma chave da Anthropic comeca com sk-ant-. Vou testar assim mesmo.' -ForegroundColor Yellow}
# Estes nomes eram da OpenAI. A 9.0 tornou a Anthropic o provedor unico, mas
# este script continuava OFERECENDO 'gpt-5.6-sol' como padrao e, pior, a lista
# $Allowed FORCAVA os tres modelos de volta para nomes da OpenAI — inclusive
# por cima de uma configuracao correta que o operador ja tivesse. O gateway
# descarta modelo que nao comeca com 'claude-', entao o sistema seguia
# funcionando com os padroes; mas o .env.local ficava gravado com nomes de um
# provedor que nao existe mais, e o diagnostico acusava configuracao antiga a
# cada execucao.
$PadraoLegal='claude-opus-5';$PadraoIntake='claude-sonnet-5';$PadraoRotina='claude-haiku-4-5-20251001'
$Legal=Read-Host ('Modelo juridico ['+$PadraoLegal+']');if(-not $Legal){$Legal=Get-Env 'JARBAS_AI_MODEL_LEGAL';if(-not $Legal){$Legal=$PadraoLegal}}
$Intake=Read-Host ('Modelo Intake PDF ['+$PadraoIntake+']');if(-not $Intake){$Intake=Get-Env 'JARBAS_AI_MODEL_INTAKE';if(-not $Intake){$Intake=$PadraoIntake}}
$Routine=Read-Host ('Modelo rotinas ['+$PadraoRotina+']');if(-not $Routine){$Routine=Get-Env 'JARBAS_AI_MODEL_ROUTINE';if(-not $Routine){$Routine=$PadraoRotina}}
# Prefixo em vez de lista fixa, igual ao gateway: um modelo Claude novo passa
# a funcionar sem editar este script, e modelo de outro fornecedor nunca passa.
if($Legal -notlike 'claude-*'){$Legal=$PadraoLegal};if($Intake -notlike 'claude-*'){$Intake=$PadraoIntake};if($Routine -notlike 'claude-*'){$Routine=$PadraoRotina}
$env:JARBAS_ROOT=$Root;$env:ANTHROPIC_API_KEY=$Key;$env:JARBAS_AI_MODEL_LEGAL=$Legal;$env:JARBAS_AI_MODEL_INTAKE=$Intake;$env:JARBAS_AI_MODEL_ROUTINE=$Routine
Write-Host 'Testando a chave antes de salvar...' -ForegroundColor Cyan;& $Python (Join-Path $Root 'tools\test_claude.py');$rc=$LASTEXITCODE
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
if($rc -ne 0){Write-Host 'A configuracao NAO foi alterada. Corrija chave, billing, permissao, internet ou modelo e tente novamente.' -ForegroundColor Red;exit $rc}
Set-Env 'ANTHROPIC_API_KEY' $Key;Set-Env 'JARBAS_AI_MODEL_LEGAL' $Legal;Set-Env 'JARBAS_AI_MODEL_INTAKE' $Intake;Set-Env 'JARBAS_AI_MODEL_ROUTINE' $Routine
Write-Host 'Claude conectada, testada e salva.' -ForegroundColor Green;& (Join-Path $Root 'PARAR_JARBAS.ps1');& (Join-Path $Root 'INICIAR_JARBAS.ps1');exit 0
