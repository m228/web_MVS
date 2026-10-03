# Downloads the latest CV module (cv_service) from the GitHub release and unpacks it next to this script.
# Called by UpdaterCV.bat. Does NOT touch cv_service\.venv or cv_service\model (not in the zip).
# Retries transient network/5xx errors (e.g. 503) a few times, and goes through the system proxy.
$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
$h = @{ 'User-Agent' = 'web_MVS-updatecv' }
$repo = 'm228/web_MVS'

# honor the system proxy (same one the browser uses) for all web calls
try {
  $sysProxy = [System.Net.WebRequest]::GetSystemWebProxy()
  $sysProxy.Credentials = [System.Net.CredentialCache]::DefaultNetworkCredentials
  [System.Net.WebRequest]::DefaultWebProxy = $sysProxy
} catch {}

function Invoke-WithRetry([scriptblock]$Action, [int]$Tries = 3) {
  for ($i = 1; $i -le $Tries; $i++) {
    try { return & $Action }
    catch {
      if ($i -eq $Tries) { throw }
      Write-Host ("Attempt {0}/{1} failed: {2} - retrying in 5s..." -f $i, $Tries, $_.Exception.Message) -ForegroundColor Yellow
      Start-Sleep -Seconds 5
    }
  }
}

Write-Host '=== Update CV: fetching latest CV module from GitHub ===' -ForegroundColor Cyan
$rel = Invoke-WithRetry { Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -Headers $h -UseBasicParsing }
$asset = $rel.assets | Where-Object { $_.name -like 'cv_service*.zip' } | Select-Object -First 1
if (-not $asset) { throw 'No cv_service_*.zip asset in the latest release' }

Write-Host ("Release: {0}  |  file: {1}  ({2:N1} KB)" -f $rel.tag_name, $asset.name, ($asset.size / 1KB))
$tmp = Join-Path $env:TEMP 'cv_service_dl.zip'
Invoke-WithRetry { Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $tmp -Headers $h -UseBasicParsing }

$dest = Join-Path $dir 'cv_service'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Expand-Archive -Path $tmp -DestinationPath $dest -Force
Remove-Item $tmp -Force
Write-Host "CV module unpacked to $dest" -ForegroundColor Green
