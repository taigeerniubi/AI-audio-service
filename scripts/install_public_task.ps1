# 注册当前用户登录时启动的部署任务，并在服务异常退出后自动重启。
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PublicOrigin
)
$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot 'run_public.ps1'
& $launcher -PublicOrigin $PublicOrigin -Check
if ($LASTEXITCODE -ne 0) { throw 'Deployment validation failed' }
$taskName = 'AI audio service gateway'
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw "Task already exists: $taskName. Review before replacing it."
}
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $launcher + '" -PublicOrigin "' + $PublicOrigin + '"') -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
$principal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'AI audio service and its authenticated frp tunnel' | Select-Object TaskName,State
Start-ScheduledTask -TaskName $taskName
