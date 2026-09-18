$ErrorActionPreference='Stop'
# Agenda o backup diario do JARBAS no Agendador de Tarefas do Windows.
#
# Backup manual e backup que nao acontece: depende de alguem lembrar no dia
# em que o escritorio esta corrido — que e exatamente o dia em que a maquina
# falha. Rode este script UMA vez, como Administrador.
#
#   .\BACKUP_AUTOMATICO.ps1              # agenda para 12:10 todo dia
#   .\BACKUP_AUTOMATICO.ps1 -Hora 19:30  # outro horario
#   .\BACKUP_AUTOMATICO.ps1 -Remover     # cancela o agendamento
param([string]$Hora='12:10',[switch]$Remover)

$Nome='JARBAS - Backup diario'
$Root=Join-Path $env:LOCALAPPDATA 'JARBAS_Enterprise'
if(-not(Test-Path $Root)){throw 'JARBAS nao instalado.'}

if($Remover){
  Unregister-ScheduledTask -TaskName $Nome -Confirm:$false -ErrorAction SilentlyContinue
  Write-Host 'Agendamento removido.' -ForegroundColor Yellow
  exit 0
}

$Script=Join-Path $Root 'scripts\BACKUP_JARBAS.ps1'
if(-not(Test-Path $Script)){throw "Script de backup nao encontrado em $Script."}

$acao=New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`""
$gatilho=New-ScheduledTaskTrigger -Daily -At $Hora
# StartWhenAvailable: se o computador estiver desligado no horario, a tarefa
# roda assim que ele ligar, em vez de simplesmente pular o dia.
$config=New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable:$false `
  -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $Nome -Action $acao -Trigger $gatilho -Settings $config `
  -Description 'Copia conferida do banco e dos documentos do JARBAS.' -Force | Out-Null

Write-Host "Backup diario agendado para as $Hora." -ForegroundColor Green
Write-Host 'Confira em algumas semanas se as copias estao sendo geradas:' -ForegroundColor Cyan
Write-Host '  Documentos\JARBAS_Backups' -ForegroundColor Cyan
Write-Host 'E teste uma restauracao ao menos uma vez por semestre.' -ForegroundColor Yellow
