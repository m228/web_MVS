# Registers a Task Scheduler task "web_MVS monitor": the receiver starts hidden at every logon of the current user and is restarted if it dies.
# Usage:  install_autostart.bat            - create the task and start it now
#         install_autostart.bat -Remove    - delete the task
#         install_autostart.bat -DryRun    - only show what would be created
param([switch]$Remove, [switch]$DryRun, [string]$TaskName = "web_MVS monitor")
$ErrorActionPreference = "Stop"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Task removed: $TaskName"
    exit 0
}

$py = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $py)) {
    Write-Host "venv not found. Run setup.bat first."
    exit 1
}
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $py -Argument "server.py" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

if ($DryRun) {
    Write-Host "Task:     $TaskName"
    Write-Host "User:     $user (at logon)"
    Write-Host "Command:  $py server.py"
    Write-Host "Folder:   $PSScriptRoot"
    exit 0
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Task created and started: $TaskName"
Write-Host "The receiver now starts hidden at every logon. Log: $PSScriptRoot\service.log"
