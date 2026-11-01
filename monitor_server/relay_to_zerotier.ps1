# Relay for plant PCs that have NO direct internet (they go out only through the server's HTTP proxy) and so cannot join ZeroTier.
# Run it on a PC that DOES have ZeroTier in the same network (for example the main server that shares the internet): it listens on its
# LAN address and forwards TCP to the monitoring receiver over ZeroTier (Windows "portproxy"). The plant app then sends its pulses to
# http://<LAN address of this PC>:8769 (tick "http в закрытой сети" there). Needs administrator rights: the script asks for them itself.
# Usage:  relay_to_zerotier.bat -Listen 192.168.1.5                    - LAN address of THIS PC that the plant can reach
#         relay_to_zerotier.bat -Listen 192.168.1.5 -Target 11.11.11.12 -Port 8769 -AllowFrom 192.168.1.0/24
#         relay_to_zerotier.bat -Listen 192.168.1.5 -Remove            - remove the relay and its firewall rule
#         add -DryRun to only show what would be done
param([string]$Listen = "", [string]$Target = "11.11.11.12", [int]$Port = 8769, [string]$AllowFrom = "", [switch]$Remove, [switch]$DryRun)
$ErrorActionPreference = "Stop"
$ruleName = "web_MVS monitor relay"

$local = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } | ForEach-Object { $_.IPAddress })
if (-not $Listen) {
    Write-Host "Say which LAN address of this PC the plant should connect to: -Listen <address>"
    Write-Host ("Addresses of this PC: " + ($local -join ", "))
    exit 1
}
if ($local -notcontains $Listen) {
    Write-Host "This PC has no address $Listen. Addresses of this PC: $($local -join ', ')"
    exit 1
}
if (-not $AllowFrom -and $Listen -match '^(\d+\.\d+\.\d+)\.\d+$') { $AllowFrom = $Matches[1] + ".0/24" }

if ($DryRun) {
    if ($Remove) {
        Write-Host "Would remove: portproxy ${Listen}:$Port and firewall rule '$ruleName'"
    } else {
        Write-Host "Would forward: ${Listen}:$Port -> ${Target}:$Port (over ZeroTier)"
        Write-Host "Would allow inbound TCP $Port to $Listen from $AllowFrom (rule '$ruleName')"
    }
    exit 0
}

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Listen", $Listen, "-Target", $Target, "-Port", $Port, "-AllowFrom", $AllowFrom)
    if ($Remove) { $argList += "-Remove" }
    Start-Process powershell -Verb RunAs -ArgumentList $argList -Wait
    exit 0
}

& netsh interface portproxy delete v4tov4 listenaddress=$Listen listenport=$Port 2>$null | Out-Null
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
if ($Remove) {
    Write-Host "Relay removed: ${Listen}:$Port"
    exit 0
}

Set-Service -Name iphlpsvc -StartupType Automatic
Start-Service -Name iphlpsvc -ErrorAction SilentlyContinue
& netsh interface portproxy add v4tov4 listenaddress=$Listen listenport=$Port connectaddress=$Target connectport=$Port
New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalAddress $Listen -LocalPort $Port -RemoteAddress $AllowFrom -Action Allow -Profile Any | Out-Null
Write-Host "Relay created: ${Listen}:$Port -> ${Target}:$Port, allowed from $AllowFrom"
& netsh interface portproxy show v4tov4
$t = Test-NetConnection -ComputerName $Target -Port $Port -WarningAction SilentlyContinue
if ($t.TcpTestSucceeded) { Write-Host "OK: the receiver $Target`:$Port answers from this PC (ZeroTier works)." }
else { Write-Host "WARNING: this PC cannot reach ${Target}:$Port. Check that ZeroTier is connected here (zerotier-cli listnetworks) and the receiver is running." }
