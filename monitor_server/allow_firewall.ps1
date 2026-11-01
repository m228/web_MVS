# Allows inbound TCP to the monitoring receiver ONLY from the ZeroTier subnet (rule "web_MVS monitor (ZeroTier)").
# Needs administrator rights: the script asks for them itself (UAC). Nothing is opened to the LAN or the internet.
# Usage:  allow_firewall.bat                      - subnet = host from data\server.json with the last octet replaced (/24)
#         allow_firewall.bat -Subnet 11.11.11.0/24
#         allow_firewall.bat -Remove              - delete the rule
param([string]$Subnet = "", [int]$Port = 0, [switch]$Remove, [switch]$DryRun)
$ErrorActionPreference = "Stop"
$ruleName = "web_MVS monitor (ZeroTier)"

$host_ = ""; $port_ = 8769
$cfgPath = Join-Path $PSScriptRoot "data\server.json"
if (Test-Path $cfgPath) {
    try {
        $j = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($j.host) { $host_ = [string]$j.host }
        if ($j.port) { $port_ = [int]$j.port }
    } catch { }
}
if ($Port -gt 0) { $port_ = $Port }
if (-not $Subnet) {
    if ($host_ -match '^(\d+\.\d+\.\d+)\.\d+$') { $Subnet = $Matches[1] + ".0/24" }
}

if (-not $Remove -and -not $Subnet) {
    Write-Host "Cannot work out the ZeroTier subnet: set host (your ZeroTier address) in data\server.json or pass -Subnet 11.11.11.0/24"
    exit 1
}
if ($DryRun) {
    Write-Host "Rule:   $ruleName"
    Write-Host ("Action: " + $(if ($Remove) { "remove" } else { "allow inbound TCP $port_ from $Subnet" }))
    exit 0
}

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    if ($Subnet) { $argList += @("-Subnet", $Subnet) }
    if ($Port -gt 0) { $argList += @("-Port", $Port) }
    if ($Remove) { $argList += "-Remove" }
    Start-Process powershell -Verb RunAs -ArgumentList $argList -Wait
    exit 0
}

Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
if ($Remove) { Write-Host "Rule removed: $ruleName"; exit 0 }
New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort $port_ -RemoteAddress $Subnet -Action Allow -Profile Any | Out-Null
Write-Host "Rule created: allow TCP $port_ from $Subnet"
