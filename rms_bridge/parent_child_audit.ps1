param(
  [string]$ConfigPath = "$PSScriptRoot\config.json",
  [string]$OutputDir = "$PSScriptRoot\exports\parent-child-audit",
  [int]$SampleRows = 15
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $ConfigPath)) {
  throw "Missing config file: $ConfigPath"
}

$config = Get-Content $ConfigPath -Raw | ConvertFrom-Json

function New-SqlConnection {
  $builder = New-Object System.Data.SqlClient.SqlConnectionStringBuilder
  $builder["Data Source"] = $config.sqlServer
  $builder["Initial Catalog"] = $config.database
  $builder["User ID"] = $config.sqlUser
  $builder["Password"] = $config.sqlPassword
  $builder["Integrated Security"] = $false
  $builder["Encrypt"] = $false
  $builder["TrustServerCertificate"] = $true
  $builder["Connect Timeout"] = 10
  return New-Object System.Data.SqlClient.SqlConnection($builder.ConnectionString)
}

function Invoke-SqlRows([string]$Query) {
  $conn = New-SqlConnection
  try {
    $conn.Open()
    $cmd = $conn.CreateCommand()
    $cmd.CommandText = $Query
    $cmd.CommandTimeout = 90
    $reader = $cmd.ExecuteReader()
    $rows = @()
    while ($reader.Read()) {
      $obj = [ordered]@{}
      for ($i = 0; $i -lt $reader.FieldCount; $i++) {
        $name = $reader.GetName($i)
        $value = if ($reader.IsDBNull($i)) { $null } else { $reader.GetValue($i) }
        if ($value -is [DateTime]) { $value = $value.ToString("o") }
        $obj[$name] = $value
      }
      $rows += [pscustomobject]$obj
    }
    $reader.Close()
    return $rows
  } finally {
    if ($conn.State -ne "Closed") { $conn.Close() }
  }
}

function Quote-SqlName([string]$Name) {
  return "[" + $Name.Replace("]", "]]") + "]"
}

New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

Write-Host "Scanning RMS schema for parent/child, kit, assembly, pack/case, and matrix relationships..." -ForegroundColor Cyan

$schemaRows = @(Invoke-SqlRows @"
SELECT
    c.TABLE_SCHEMA,
    c.TABLE_NAME,
    c.ORDINAL_POSITION,
    c.COLUMN_NAME,
    c.DATA_TYPE
FROM INFORMATION_SCHEMA.COLUMNS c
ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION;
"@)

$schemaRows | Export-Csv -Path (Join-Path $OutputDir "schema.csv") -NoTypeInformation -Encoding UTF8

$keywords = @(
  "parent","child","component","kit","assembly","pack","case",
  "matrix","master","detail","member","conversion","factor","units"
)

$candidateTableNames = @{}

foreach ($row in $schemaRows) {
  $tableKey = "$($row.TABLE_SCHEMA).$($row.TABLE_NAME)"
  $tableText = "$($row.TABLE_NAME) $($row.COLUMN_NAME)".ToLowerInvariant()
  $score = 0
  foreach ($kw in $keywords) {
    if ($tableText.Contains($kw)) { $score++ }
  }
  if ($score -gt 0) {
    if (-not $candidateTableNames.ContainsKey($tableKey)) {
      $candidateTableNames[$tableKey] = 0
    }
    $candidateTableNames[$tableKey] += $score
  }
}

$candidates = @()
foreach ($key in $candidateTableNames.Keys) {
  $parts = $key.Split('.',2)
  $schema = $parts[0]
  $table = $parts[1]
  $cols = @($schemaRows | Where-Object { $_.TABLE_SCHEMA -eq $schema -and $_.TABLE_NAME -eq $table })
  $colNames = @($cols | ForEach-Object { [string]$_.COLUMN_NAME })
  $lower = @($colNames | ForEach-Object { $_.ToLowerInvariant() })

  $idLike = @($lower | Where-Object {
    $_ -match '(^|_)(item|parent|child|component|member|master|detail).*id$' -or
    $_ -match '^(parent|child|component|item|member|master|detail)id$'
  })
  $qtyLike = @($lower | Where-Object {
    $_ -match 'quantity|qty|factor|conversion|units|count|pack|case'
  })

  $candidates += [pscustomobject]@{
    TableSchema = $schema
    TableName = $table
    KeywordScore = [int]$candidateTableNames[$key]
    IDLikeColumns = ($idLike -join '|')
    QuantityLikeColumns = ($qtyLike -join '|')
    Columns = ($colNames -join '|')
  }
}

$candidates = @($candidates | Sort-Object @{Expression="KeywordScore";Descending=$true}, TableName)
$candidates | Export-Csv -Path (Join-Path $OutputDir "relationship_table_candidates.csv") -NoTypeInformation -Encoding UTF8

Write-Host "Candidate relationship tables: $($candidates.Count)" -ForegroundColor Green
$candidates | Select-Object -First 30 | Format-Table -AutoSize

$interesting = @(
  $candidates | Where-Object {
    $_.IDLikeColumns -and $_.QuantityLikeColumns
  } | Select-Object -First 20
)

$sampleIndex = @()
foreach ($cand in $interesting) {
  $schema = [string]$cand.TableSchema
  $table = [string]$cand.TableName
  $safe = "$(Quote-SqlName $schema).$(Quote-SqlName $table)"
  $safeFile = ($schema + "_" + $table) -replace '[^A-Za-z0-9_.-]','_'

  try {
    $count = @(Invoke-SqlRows "SELECT COUNT_BIG(*) AS RowCount FROM $safe;")
    $rowCount = if ($count.Count) { [int64]$count[0].RowCount } else { 0 }

    $sample = @()
    if ($rowCount -gt 0) {
      $take = [Math]::Max(1,[Math]::Min($SampleRows,100))
      $sample = @(Invoke-SqlRows "SELECT TOP $take * FROM $safe;")
      $sample | Export-Csv -Path (Join-Path $OutputDir ("sample_" + $safeFile + ".csv")) -NoTypeInformation -Encoding UTF8
    }

    $sampleIndex += [pscustomobject]@{
      TableSchema = $schema
      TableName = $table
      RowCount = $rowCount
      SampleFile = if ($rowCount -gt 0) { "sample_$safeFile.csv" } else { "" }
    }
  } catch {
    $sampleIndex += [pscustomobject]@{
      TableSchema = $schema
      TableName = $table
      RowCount = ""
      SampleFile = ""
      Error = $_.Exception.Message
    }
  }
}

$sampleIndex | Export-Csv -Path (Join-Path $OutputDir "sample_index.csv") -NoTypeInformation -Encoding UTF8

# Also export stock-on-hand item reference data for later joining once the exact
# relationship table is identified. Item.Quantity is RMS on-hand quantity.
$itemStock = @(Invoke-SqlRows @"
SELECT
    I.ID AS ItemID,
    I.ItemLookupCode AS SKU,
    I.Description,
    I.Quantity AS OnHandQuantity,
    I.ReorderPoint,
    I.RestockLevel,
    I.Inactive,
    I.DoNotOrder,
    I.LastSold,
    I.LastReceived
FROM Item I
WHERE ISNULL(I.Quantity,0) <> 0
ORDER BY I.ItemLookupCode;
"@)
$itemStock | Export-Csv -Path (Join-Path $OutputDir "items_with_on_hand.csv") -NoTypeInformation -Encoding UTF8

Write-Host ""
Write-Host "Parent/child audit discovery complete." -ForegroundColor Green
Write-Host "Output folder: $OutputDir"
Write-Host "Review relationship_table_candidates.csv and sample_index.csv first."
Write-Host "No RMS or Lightspeed records were modified." -ForegroundColor Green
