$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new() } catch {}
$Root=Join-Path $env:LOCALAPPDATA 'JARBAS_Enterprise'
$EnvFile=Join-Path $Root '.env.local'
$Python=Join-Path $Root 'runtime\python.exe'
$RunScript=Join-Path $Root 'tools\run_server.py'
$LogDir=Join-Path $Root 'logs'
$PidFile=Join-Path $Root 'JARBAS.pid'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
function Load-Env {if(-not(Test-Path $EnvFile)){throw 'Arquivo .env.local nao encontrado.'};Get-Content $EnvFile | ForEach-Object {$line=$_.Trim();if($line -and -not $line.StartsWith('#') -and $line.Contains('=')){$parts=$line.Split('=',2);[Environment]::SetEnvironmentVariable($parts[0],$parts[1],'Process')}}}
function Set-EnvValue([string]$Key,[string]$Value){$lines=@();if(Test-Path $EnvFile){$lines=Get-Content $EnvFile};$found=$false;$out=New-Object System.Collections.Generic.List[string];foreach($line in $lines){if($line -match ('^\s*'+[regex]::Escape($Key)+'\s*=')){$out.Add("$Key=$Value");$found=$true}else{$out.Add($line)}};if(-not $found){$out.Add("$Key=$Value")};[IO.File]::WriteAllLines($EnvFile,$out,[Text.UTF8Encoding]::new($false))}
function Test-Health([int]$Port){try{$r=Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$Port/health" -TimeoutSec 2;return $r.StatusCode -eq 200}catch{return $false}}
function Test-PortFree([int]$Port){try{$l=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,$Port);$l.Start();$l.Stop();return $true}catch{return $false}}
function Find-FreePort {foreach($p in 8765..8799){if(Test-PortFree $p){return $p}};throw 'Nenhuma porta livre entre 8765 e 8799.'}
Load-Env
if(-not(Test-Path $Python)){throw 'Runtime Python do JARBAS nao encontrado. Execute o instalador completo.'}
if(-not(Test-Path $RunScript)){throw 'Inicializador Python do JARBAS nao encontrado.'}
$env:JARBAS_ROOT=$Root
& $Python -c "import sys; sys.path.insert(0,r'$Root'); import app.main; print('IMPORT_APP_OK')" *> (Join-Path $LogDir 'prestart-import.log')
if($LASTEXITCODE -ne 0){Write-Host 'Falha ao importar o nucleo. Execute DIAGNOSTICO_JARBAS.cmd.' -ForegroundColor Red;exit 11}
$Port=8765;if($env:JARBAS_PORT -and $env:JARBAS_PORT -match '^\d+$'){$Port=[int]$env:JARBAS_PORT}
if(Test-Health $Port){Start-Process "http://127.0.0.1:$Port/login";exit 0}
if(Test-Path $PidFile){$OldProcessId=Get-Content $PidFile|Select-Object -First 1;if($OldProcessId -match '^\d+$'){$proc=Get-Process -Id ([int]$OldProcessId) -ErrorAction SilentlyContinue;if($proc){Stop-Process -Id ([int]$OldProcessId) -Force -ErrorAction SilentlyContinue}};Remove-Item $PidFile -Force -ErrorAction SilentlyContinue}
if(-not(Test-PortFree $Port)){$Port=Find-FreePort;Set-EnvValue 'JARBAS_PORT' "$Port";[Environment]::SetEnvironmentVariable('JARBAS_PORT',"$Port",'Process')}
$Out=Join-Path $LogDir 'jarbas-out.log';$Err=Join-Path $LogDir 'jarbas-error.log';Set-Content $Out '' -Encoding UTF8;Set-Content $Err '' -Encoding UTF8
$JarbasProcess=Start-Process -FilePath $Python -ArgumentList @("`"$RunScript`"") -WorkingDirectory $Root -RedirectStandardOutput $Out -RedirectStandardError $Err -WindowStyle Hidden -PassThru
[IO.File]::WriteAllText($PidFile,"$($JarbasProcess.Id)`r`n",[Text.ASCIIEncoding]::new())
for($i=0;$i -lt 120;$i++){Start-Sleep -Seconds 1;if(Test-Health $Port){[IO.File]::WriteAllText((Join-Path $Root 'PORTA_LOCAL.txt'),"$Port`r`n",[Text.ASCIIEncoding]::new());Start-Process "http://127.0.0.1:$Port/login";exit 0};try{$JarbasProcess.Refresh()}catch{};if($JarbasProcess.HasExited){break}}
Write-Host 'O JARBAS nao respondeu ao health-check.' -ForegroundColor Red;if(Test-Path $Err){Get-Content $Err -Tail 120|Write-Host};Write-Host "Execute DIAGNOSTICO_JARBAS.cmd em $Root" -ForegroundColor Yellow;exit 21
