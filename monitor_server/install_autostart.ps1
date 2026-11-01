# Registers a Task Scheduler task "web_MVS monitor": the receiver starts hidden at every logon of the current user and is restarted if it dies.
# Usage:  install_autostart.bat            - create the task and start it now
#         install_autostart.bat -Remove    - delete the task
#         install_autostart.bat -DryRun    - only show what would be created
#         install_autostart.bat -NoTray    - old way: server.py without the tray icon
# By default the task runs tray.py: an icon in the tray (open the panel / restart the server / exit) that keeps server.py running.
param([switch]$Remove, [switch]$DryRun, [switch]$NoTray, [string]$TaskName = "web_MVS monitor")
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
$script = if ($NoTray) { "server.py" } else { "tray.py" }
$action = New-ScheduledTaskAction -Execute $py -Argument $script -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

if ($DryRun) {
    Write-Host "Task:     $TaskName"
    Write-Host "User:     $user (at logon)"
    Write-Host "Command:  $py $script"
    Write-Host "Folder:   $PSScriptRoot"
    exit 0
}

# the old instance (server.py or tray.py) holds the port: stop it first, otherwise the new one cannot start
$old = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($old -and $old.State -eq "Running") { Stop-ScheduledTask -TaskName $TaskName; Start-Sleep -Seconds 2 }
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Task created and started: $TaskName"
Write-Host "The receiver now starts hidden at every logon. Log: $PSScriptRoot\service.log"
