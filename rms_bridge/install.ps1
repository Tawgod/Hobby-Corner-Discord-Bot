param([string]$InstallDir = "C:\HobbyCorner\RMSBridge")
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item "$PSScriptRoot\bridge.ps1" "$InstallDir\bridge.ps1" -Force
if (-not (Test-Path "$InstallDir\config.json")) {
  Copy-Item "$PSScriptRoot\config.example.json" "$InstallDir\config.json"
}
Write-Host ""
Write-Host "Installed RMS Bridge to $InstallDir" -ForegroundColor Green
Write-Host "Edit $InstallDir\config.json, then run:"
Write-Host "powershell -ExecutionPolicy Bypass -File $InstallDir\bridge.ps1 -Action test"
