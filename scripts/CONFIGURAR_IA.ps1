$ErrorActionPreference='Stop'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new() } catch {}
$Root=Join-Path $env:LOCALAPPDATA 'JARBAS_Enterprise';$EnvFile=Join-Path $Root '.env.local';$Python=Join-Path $Root 'runtime\python.exe'
function SecretPlain([string]$Prompt){$s=Read-Host $Prompt -AsSecureString;$p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s);try{return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($p)}finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p)}}
function Get-Env([string]$Key){if(-not(Test-Path $EnvFile)){return ''};$line=Get-Content $EnvFile|Where-Object{$_ -match ('^\s*'+[regex]::Escape($Key)+'\s*=')}|Select-Object -First 1;if($line){return $line.Split('=',2)[1]};return ''}
function Set-Env([string]$Key,[string]$Value){$lines=@();if(Test-Path $EnvFile){$lines=Get-Content $EnvFile};$found=$false;$out=New-Object System.Collections.Generic.List[string];foreach($line in $lines){if($line -match ('^\s*'+[regex]::Escape($Key)+'\s*=')){$out.Add("$Key=$Value");$found=$true}else{$out.Add($line)}};if(-not $found){$out.Add("$Key=$Value")};[IO.File]::WriteAllLines($EnvFile,$out,[Text.UTF8Encoding]::new($false))}
if(-not(Test-Path $Root)){throw 'JARBAS nao instalado.'};if(-not(Test-Path $Python)){throw 'Runtime Python nao encontrado.'}
Write-Host '============================================================' -ForegroundColor Cyan;Write-Host ' JARBAS 9.0.2 - ASSISTENTE DE CONEXAO OPENAI' -ForegroundColor Cyan;Write-Host '============================================================' -ForegroundColor Cyan
$Existing=Get-Env 'ANTHROPIC_API_KEY';if($Existing){Write-Host ('Chave atual configurada: ****'+$Existing.Substring([Math]::Max(0,$Existing.Length-4))) -ForegroundColor Green}
$Key=SecretPlain 'Cole uma NOVA Claude API Key (Enter testa/mantem a atual)';if(-not $Key){$Key=$Existing};if(-not $Key){throw 'Nenhuma chave Claude foi informada.'}
$Legal=Read-Host 'Modelo juridico [gpt-5.6-sol]';if(-not $Legal){$Legal=Get-Env 'JARBAS_AI_MODEL_LEGAL';if(-not $Legal){$Legal='gpt-5.6-sol'}}
$Intake=Read-Host 'Modelo Intake PDF [gpt-5.6-terra]';if(-not $Intake){$Intake=Get-Env 'JARBAS_AI_MODEL_INTAKE';if(-not $Intake){$Intake='gpt-5.6-terra'}}
$Routine=Read-Host 'Modelo rotinas [gpt-5.6-terra]';if(-not $Routine){$Routine=Get-Env 'JARBAS_AI_MODEL_ROUTINE';if(-not $Routine){$Routine='gpt-5.6-terra'}}
$Allowed=@('gpt-5.6-sol','gpt-5.6-terra','gpt-5.6-luna');if($Allowed -notcontains $Legal){$Legal='gpt-5.6-sol'};if($Allowed -notcontains $Intake){$Intake='gpt-5.6-terra'};if($Allowed -notcontains $Routine){$Routine='gpt-5.6-terra'}
$env:JARBAS_ROOT=$Root;$env:ANTHROPIC_API_KEY=$Key;$env:JARBAS_AI_MODEL_LEGAL=$Legal;$env:JARBAS_AI_MODEL_INTAKE=$Intake;$env:JARBAS_AI_MODEL_ROUTINE=$Routine
Write-Host 'Testando a chave antes de salvar...' -ForegroundColor Cyan;& $Python (Join-Path $Root 'tools\test_claude.py');$rc=$LASTEXITCODE
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
if($rc -ne 0){Write-Host 'A configuracao NAO foi alterada. Corrija chave, billing, permissao, internet ou modelo e tente novamente.' -ForegroundColor Red;exit $rc}
Set-Env 'ANTHROPIC_API_KEY' $Key;Set-Env 'JARBAS_AI_MODEL_LEGAL' $Legal;Set-Env 'JARBAS_AI_MODEL_INTAKE' $Intake;Set-Env 'JARBAS_AI_MODEL_ROUTINE' $Routine
Write-Host 'Claude conectada, testada e salva.' -ForegroundColor Green;& (Join-Path $Root 'PARAR_JARBAS.ps1');& (Join-Path $Root 'INICIAR_JARBAS.ps1');exit 0
