# Downloads the latest motion service (motion_service) from the GitHub release and unpacks it next to this script.
# Called by UpdaterMotion.bat. Does NOT touch motion_service\.venv or motion_service\data (settings; not in the zip).
# Retries transient network/5xx errors (e.g. 503) a few times, and goes through the system proxy.
# -ZipPath <file>: unpack a motion_service_v*.zip downloaded on another PC instead (when this machine
#   sees the release but cannot download it: the provider cuts release-assets.githubusercontent.com).
param([string]$ZipPath = '')
$ErrorActionPreference = 'Stop'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
$dir = $PSScriptRoot
$h = @{ 'User-Agent' = 'web_MVS-updatemotion' }
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

$tmp = $null
if ($ZipPath) {
  if (-not (Test-Path -LiteralPath $ZipPath)) { throw "Zip not found: $ZipPath" }
  $zip = (Resolve-Path -LiteralPath $ZipPath).Path
  Write-Host "=== Update Motion: unpacking local file $zip ===" -ForegroundColor Cyan
} else {
  Write-Host '=== Update Motion: fetching latest motion service from GitHub ===' -ForegroundColor Cyan
  $rel = Invoke-WithRetry { Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -Headers $h -UseBasicParsing }
  $asset = $rel.assets | Where-Object { $_.name -like 'motion_service*.zip' } | Select-Object -First 1
  if (-not $asset) { throw 'No motion_service_*.zip asset in the latest release' }

  Write-Host ("Release: {0}  |  file: {1}  ({2:N1} KB)" -f $rel.tag_name, $asset.name, ($asset.size / 1KB))
  $tmp = Join-Path $env:TEMP 'motion_service_dl.zip'
  try { Invoke-WithRetry { Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $tmp -Headers $h -UseBasicParsing } }
  catch {
    Write-Host 'Release found, but the file did not download (GitHub file host blocked from this machine?).' -ForegroundColor Yellow
    Write-Host ("Download it on another PC: {0}" -f $asset.browser_download_url) -ForegroundColor Yellow
    Write-Host ('then run here: UpdaterMotion.bat -ZipPath "<path to {0}>"' -f $asset.name) -ForegroundColor Yellow
    throw
  }
  $zip = $tmp
}

# a wrong zip (web_MVS / cv_service) must not land in motion_service\
Add-Type -AssemblyName System.IO.Compression.FileSystem
$za = [System.IO.Compression.ZipFile]::OpenRead($zip)
try { $ok = @($za.Entries | Where-Object { $_.FullName -eq 'motion_engine.py' }).Count -gt 0 } finally { $za.Dispose() }
if (-not $ok) { throw "$zip is not a motion_service zip (no motion_engine.py inside)" }

$dest = Join-Path $dir 'motion_service'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Expand-Archive -LiteralPath $zip -DestinationPath $dest -Force
if ($tmp) { Remove-Item $tmp -Force }
Write-Host "Motion service unpacked to $dest" -ForegroundColor Green
