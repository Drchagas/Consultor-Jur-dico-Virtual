$ErrorActionPreference='Stop'
$Root=Join-Path $env:LOCALAPPDATA 'JARBAS_Enterprise';$Python=Join-Path $Root 'runtime\python.exe';$EnvFile=Join-Path $Root '.env.local'
function SecretPlain([string]$Prompt){$s=Read-Host $Prompt -AsSecureString;$p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s);try{return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($p)}finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p)}}
if(-not(Test-Path $Python)){throw 'Runtime Python nao encontrado.'}
$email=Read-Host 'E-mail [admin@chagasadvogados.local]';if(-not $email){$email='admin@chagasadvogados.local'}
$p1='';for($i=0;$i -lt 5;$i++){$a=SecretPlain 'Nova senha (minimo 12 caracteres)';$b=SecretPlain 'Confirme a nova senha';if($a.Length -ge 12 -and $a -ceq $b){$p1=$a;break};Write-Host 'Senhas invalidas/diferentes.' -ForegroundColor Yellow};if(-not $p1){throw 'Senha nao definida.'}
& (Join-Path $Root 'PARAR_JARBAS.ps1');$env:JARBAS_ROOT=$Root;$env:JARBAS_RESET_EMAIL=$email;$env:JARBAS_RESET_PASSWORD=$p1;& $Python (Join-Path $Root 'tools\reset_password.py');if($LASTEXITCODE -ne 0){throw 'Falha ao atualizar senha.'};Remove-Item Env:JARBAS_RESET_PASSWORD -ErrorAction SilentlyContinue
& (Join-Path $Root 'INICIAR_JARBAS.ps1');if($LASTEXITCODE -ne 0){throw 'Senha alterada, porem o servidor nao iniciou.'}
if($email.ToLower() -eq 'admin@chagasadvogados.local'){$port=(Get-Content $EnvFile|Where-Object{$_ -match '^JARBAS_PORT='}|Select-Object -First 1).Split('=',2)[1];$env:JARBAS_PORT=$port;$env:JARBAS_ADMIN_EMAIL=$email;$env:JARBAS_LIVE_TEST_PASSWORD=$p1;& $Python (Join-Path $Root 'tools\live_login_check.py');$rc=$LASTEXITCODE;Remove-Item Env:JARBAS_LIVE_TEST_PASSWORD -ErrorAction SilentlyContinue;if($rc -ne 0){throw 'A senha foi gravada, mas o login HTTP real nao foi validado.'}}
Write-Host 'Senha alterada e validada.' -ForegroundColor Green
