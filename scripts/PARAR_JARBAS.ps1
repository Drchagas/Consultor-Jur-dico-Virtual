$ErrorActionPreference='Continue'
$Root=Join-Path $env:LOCALAPPDATA 'JARBAS_Enterprise';$PidFile=Join-Path $Root 'JARBAS.pid'
if(Test-Path $PidFile){$idText=Get-Content $PidFile|Select-Object -First 1;if($idText -match '^\d+$'){Stop-Process -Id ([int]$idText) -Force -ErrorAction SilentlyContinue};Remove-Item $PidFile -Force -ErrorAction SilentlyContinue}
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue|Where-Object{($_.Name -in @('python.exe','pythonw.exe')) -and $_.CommandLine -and $_.CommandLine -like '*JARBAS_Enterprise*tools*run_server.py*'}|ForEach-Object{Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}
Write-Host 'JARBAS encerrado.' -ForegroundColor Green
