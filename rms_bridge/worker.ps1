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
$action = ([string]$claim.job.job_type).Trim().ToLowerInvariant()
$args = $claim.job.args

$allowedActions = @(
  "test","schema","product-schema","database-schema","database-tables","database-access",
  "database-relations","migration-discovery","operational-discovery","source-tables","source-schema","table-read","read-catalog","read-query",
  "safety-test","product-web-diagnostic","table-preview","table-count","customers-preview",
  "product-export","snapshot"
)
if ($allowedActions -notcontains $action) {
  throw "Remote worker rejected unsupported bridge action: $action"
}

$tableName = if ($args -and $args.table_name) { [string]$args.table_name } else { "" }
$previewRows = if ($args -and $args.preview_rows) { [int]$args.preview_rows } else { 25 }
$queryName = if ($args -and $args.query_name) { [string]$args.query_name } else { "" }
$dataSource = if ($args -and $args.data_source) { [string]$args.data_source } else { "store" }
if (@("store","hq") -notcontains $dataSource) { throw "Unsupported data_source: $dataSource" }
$itemLookupCode = if ($args -and $args.item_lookup_code) { [string]$args.item_lookup_code } else { "" }
$customerId = if ($args -and $args.customer_id) { [string]$args.customer_id } else { "" }
$supplierId = if ($args -and $args.supplier_id) { [string]$args.supplier_id } else { "" }
$startDate = if ($args -and $args.start_date) { [string]$args.start_date } else { "" }
$endDate = if ($args -and $args.end_date) { [string]$args.end_date } else { "" }
$limit = if ($args -and $args.limit) { [int]$args.limit } else { 250 }
$resultText = ""
$errorText = ""
$status = "success"

try {
  $bridgeArgs = @("-NoProfile","-ExecutionPolicy","Bypass","-File",$bridgePath,"-Action",$action)
  if ($tableName) { $bridgeArgs += @("-TableName",$tableName) }
  if ($action -in @("source-tables","source-schema","table-read")) { $bridgeArgs += @("-DataSource",$dataSource) }
  if ($action -eq "table-read") { $bridgeArgs += @("-Limit",[string]$limit) }
  if ($action -eq "table-preview") { $bridgeArgs += @("-PreviewRows",[string]$previewRows) }
  if ($action -eq "read-query") {
    if (-not $queryName) { throw "read-query job is missing query_name." }
    $bridgeArgs += @("-QueryName",$queryName,"-Limit",[string]$limit)
    if ($itemLookupCode) { $bridgeArgs += @("-ItemLookupCode",$itemLookupCode) }
    if ($customerId) { $bridgeArgs += @("-CustomerID",$customerId) }
    if ($supplierId) { $bridgeArgs += @("-SupplierID",$supplierId) }
    if ($startDate) { $bridgeArgs += @("-StartDate",$startDate) }
    if ($endDate) { $bridgeArgs += @("-EndDate",$endDate) }
  }
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
