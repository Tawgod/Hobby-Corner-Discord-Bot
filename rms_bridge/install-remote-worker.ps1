param(
  [string]$InstallDir = "C:\HobbyCorner\RMSBridge",
  [int]$EveryMinutes = 2
)

$ErrorActionPreference = "Stop"
if ($EveryMinutes -lt 1) { throw "EveryMinutes must be at least 1." }

$workerSource = Join-Path $PSScriptRoot "worker.ps1"
$workerDest = Join-Path $InstallDir "worker.ps1"
$bridgeSource = Join-Path $PSScriptRoot "bridge.ps1"

if (-not (Test-Path $InstallDir)) {
  New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
}
if (-not (Test-Path $workerSource)) { throw "Missing worker.ps1 beside this installer." }
Copy-Item $workerSource $workerDest -Force

if (Test-Path $bridgeSource) {
  Copy-Item $bridgeSource (Join-Path $InstallDir "bridge.ps1") -Force
}

if (-not (Test-Path (Join-Path $InstallDir "config.json"))) {
  throw "Existing config.json was not found in $InstallDir. Install/configure the RMS Bridge first."
}

$taskName = "HobbyCorner RMS Bridge Worker"
$taskCmd = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + $workerDest + '"'
& schtasks.exe /Create /TN $taskName /TR $taskCmd /SC MINUTE /MO $EveryMinutes /RU SYSTEM /F | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Could not create scheduled task. Run PowerShell as Administrator." }

& schtasks.exe /Run /TN $taskName | Out-Host
Write-Host ""
Write-Host "Remote RMS worker installed." -ForegroundColor Green
Write-Host "Task: $taskName"
Write-Host "Checks Railway every $EveryMinutes minute(s)."
Write-Host "The task has also been started once now."
