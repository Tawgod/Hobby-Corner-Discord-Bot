param(
  [string]$InstallDir = "C:\HobbyCorner\RMSBridge"
)

$ErrorActionPreference = "Stop"
$configPath = Join-Path $InstallDir "config.json"
$bridgePath = Join-Path $InstallDir "bridge.ps1"

if (-not (Test-Path $configPath)) { throw "Missing config: $configPath" }
if (-not (Test-Path $bridgePath)) { throw "Missing bridge: $bridgePath" }

$config = Get-Content $configPath -Raw | ConvertFrom-Json
$base = $config.apiBaseUrl.TrimEnd("/")
$headers = @{
  "x-admin-key" = $config.apiKey
  "x-worker-name" = $env:COMPUTERNAME
}

try {
  $claim = Invoke-RestMethod -Method Post -Uri "$base/bridge/rms-jobs/claim" -Headers $headers
} catch {
  Write-Error "Could not check Railway job queue: $($_.Exception.Message)"
  exit 1
}

if ($null -eq $claim.job) {
  exit 0
}

$jobId = [int]$claim.job.id
$action = [string]$claim.job.job_type
$args = $claim.job.args
$tableName = if ($args -and $args.table_name) { [string]$args.table_name } else { "" }
$previewRows = if ($args -and $args.preview_rows) { [int]$args.preview_rows } else { 25 }
$resultText = ""
$errorText = ""
$status = "success"

try {
  $bridgeArgs = @("-NoProfile","-ExecutionPolicy","Bypass","-File",$bridgePath,"-Action",$action)
  if ($tableName) { $bridgeArgs += @("-TableName",$tableName) }
  if ($action -eq "table-preview") { $bridgeArgs += @("-PreviewRows",[string]$previewRows) }
  $resultText = (& powershell.exe @bridgeArgs 2>&1 | Out-String)
  if ($LASTEXITCODE -ne 0) {
    $status = "failed"
    $errorText = $resultText
  }
} catch {
  $status = "failed"
  $errorText = $_ | Out-String
}

$body = @{
  status = $status
  worker_name = $env:COMPUTERNAME
  result_text = $resultText
  error_text = $errorText
} | ConvertTo-Json -Depth 4

try {
  Invoke-RestMethod -Method Post -Uri "$base/bridge/rms-jobs/$jobId/complete" -Headers @{ "x-admin-key" = $config.apiKey } -ContentType "application/json" -Body $body | Out-Null
} catch {
  Write-Error "Job ran but status could not be reported: $($_.Exception.Message)"
  exit 2
}

if ($status -eq "failed") { exit 1 }
exit 0
