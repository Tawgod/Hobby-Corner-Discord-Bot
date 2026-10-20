# Optional, standalone reporting companion for the existing RMS bridge.
# Does not import, modify, or invoke bridge.ps1, and does not send data to migration endpoints.
param(
  [ValidateSet('schema-check','inventory','sales','all')]
  [string]$Action = 'schema-check',
  [string]$ConfigPath = "$PSScriptRoot\config.json",
  [string]$OutputDir = "$PSScriptRoot\reports",
  [datetime]$From = [datetime]::Today.AddDays(-31),
  [datetime]$To = [datetime]::Today.AddDays(1)
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $ConfigPath)) { throw "Missing bridge config: $ConfigPath" }
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
if ($From -ge $To) { throw 'From must precede To (exclusive).' }

function Open-RmsConnection {
  $b = New-Object System.Data.SqlClient.SqlConnectionStringBuilder
  $b['Data Source'] = $config.sqlServer
  $b['Initial Catalog'] = $config.database
  $b['User ID'] = $config.sqlUser
  $b['Password'] = $config.sqlPassword
  $b['Integrated Security'] = $false
  $b['Connect Timeout'] = 15
  # Follow existing local bridge connection settings; review TLS separately.
  $b['Encrypt'] = $false
  $b['TrustServerCertificate'] = $true
  $c = New-Object System.Data.SqlClient.SqlConnection($b.ConnectionString)
  $c.Open()
  return $c
}
function Read-Rows([System.Data.SqlClient.SqlConnection]$Connection, [string]$Sql, [hashtable]$Parameters = @{}) {
  $command = $Connection.CreateCommand()
  $command.CommandText = $Sql
  $command.CommandTimeout = 120
  foreach($name in $Parameters.Keys) {
    $parameter = $command.Parameters.Add("@$name",[System.Data.SqlDbType]::DateTime)
    $parameter.Value = $Parameters[$name]
  }
  $reader = $command.ExecuteReader()
  try {
    $result = New-Object System.Collections.Generic.List[object]
    while($reader.Read()) {
      $row = [ordered]@{}
      for($i=0;$i -lt $reader.FieldCount;$i++) {
        $row[$reader.GetName($i)] = if ($reader.IsDBNull($i)) { $null } else { $reader.GetValue($i) }
      }
      $result.Add([pscustomobject]$row)
    }
    return $result.ToArray()
  } finally { $reader.Close(); $command.Dispose() }
}
function Require-Columns($Schema,[string]$Table,[string[]]$Columns) {
  $available=@($Schema | Where-Object { $_.TABLE_NAME -eq $Table -and $_.TABLE_SCHEMA -eq 'dbo' } | ForEach-Object { $_.COLUMN_NAME })
  if(-not $available.Count) { throw "Missing dbo.$Table. Inspect schema-check before running exports." }
  foreach($column in $Columns) {
    if($available -notcontains $column) { throw "dbo.$Table missing required column $column; inspect schema-check." }
  }
}
function Save-Report($Rows,[string]$Filename) {
  if (-not (Test-Path -LiteralPath $OutputDir)) { New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null }
  $path = Join-Path $OutputDir $Filename
  # Always persist an explicit header even when SQL returns no rows.
  if (@($Rows).Count -eq 0) {
    "No rows for requested scope" | Set-Content -LiteralPath $path -Encoding UTF8
  } else { $Rows | Export-Csv -LiteralPath $path -NoTypeInformation -Encoding UTF8 }
  Write-Host "Wrote $path ($(@($Rows).Count) rows)"
}
$connection=Open-RmsConnection
try {
  $schema=@(Read-Rows $connection "SELECT TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME IN ('Item','Department','Category','Supplier','Transaction','TransactionEntry','PUBLIC_Transaction','PUBLIC_TransactionEntry') ORDER BY TABLE_NAME,ORDINAL_POSITION")
  if($Action -eq 'schema-check') {
    $schema | Format-Table -AutoSize
    Write-Host 'Schema inspected only. No file export, SQL mutation, or upload performed.'
    return
  }
  if($Action -in @('inventory','all')) {
    Require-Columns $schema 'Item' @('ID','DepartmentID','CategoryID','Quantity','Cost','Price')
    Require-Columns $schema 'Department' @('ID','Name')
    Require-Columns $schema 'Category' @('ID','Name')
    # Supplier and brand are intentionally omitted until their live RMS joins are validated.
    $sql=@'
SELECT CAST(GETDATE() AS datetime) AS SnapshotAt,
       I.DepartmentID, COALESCE(D.Name,'Unassigned') AS Department,
       I.CategoryID, COALESCE(C.Name,'Unassigned') AS Category,
       COUNT_BIG(*) AS SkuCount,
       SUM(CAST(ISNULL(I.Quantity,0) AS decimal(19,4))) AS OnHandUnits,
       SUM(CAST(ISNULL(I.Quantity,0) AS decimal(19,4)) * CAST(ISNULL(I.Cost,0) AS decimal(19,4))) AS InventoryCost,
       SUM(CAST(ISNULL(I.Quantity,0) AS decimal(19,4)) * CAST(ISNULL(I.Price,0) AS decimal(19,4))) AS InventoryRetail
FROM dbo.Item I
LEFT JOIN dbo.Department D ON D.ID = I.DepartmentID
LEFT JOIN dbo.Category C ON C.ID = I.CategoryID
GROUP BY I.DepartmentID,D.Name,I.CategoryID,C.Name
ORDER BY D.Name,C.Name;
'@
    $rows=@(Read-Rows $connection $sql)
    Save-Report $rows ('inventory_'+(Get-Date -Format 'yyyyMMdd_HHmmss')+'.csv')
  }
  if($Action -in @('sales','all')) {
    # Never reinterpret a rewards/customer-only export as complete store sales.
    # Schema preflight first; sales totals require validation of line items, voids,
    # discounts, returns, tax, and transaction types before any financial export.
    $salesTables=@($schema | Where-Object { $_.TABLE_NAME -match 'Transaction' } | Select-Object -ExpandProperty TABLE_NAME -Unique)
    if(-not $salesTables.Count) { throw 'Sales transaction tables not found.' }
    Write-Warning ('Sales export intentionally blocked pending transaction-line reconciliation. Available transaction tables: '+($salesTables -join ', '))
    if ($Action -eq 'sales') { throw 'Sales data contract is not yet verified: export refused.' }
  }
} finally { $connection.Dispose() }
