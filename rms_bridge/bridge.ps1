param(
  [ValidateSet("test","schema","customers-preview","snapshot")]
  [string]$Action = "test",
  [string]$ConfigPath = "$PSScriptRoot\config.json"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $ConfigPath)) {
  throw "Missing config file: $ConfigPath. Copy config.example.json to config.json and fill in the values."
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
    $cmd.CommandTimeout = 60
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

function Invoke-BridgePost([hashtable]$Body) {
  $headers = @{ "x-admin-key" = $config.apiKey }
  $json = $Body | ConvertTo-Json -Depth 8 -Compress
  $uri = "$($config.apiBaseUrl.TrimEnd('/'))/admin/migration/rms-snapshot"
  return Invoke-RestMethod -Method Post -Uri $uri -Headers $headers -ContentType "application/json" -Body $json
}

$customerQuery = @"
SELECT
    C.ID                 AS RMSCustomerID,
    C.FirstName,
    C.LastName,
    C.Company,
    C.EmailAddress,
    C.PhoneNumber,
    C.FaxNumber,
    C.Address,
    C.Address2,
    C.City,
    C.State,
    C.Zip,
    C.Country,
    C.TaxNumber,
    C.TaxExempt,
    C.AccountBalance,
    C.TotalSales,
    C.AccountOpened,
    C.LastVisit,
    C.TotalVisits,
    C.TotalSavings,
    C.CurrentDiscount    AS LegacyDiscount,
    C.Vouchers,
    C.LastUpdated
FROM Customer C
ORDER BY C.ID;
"@

$transactionQuery = @"
SELECT
    T.CustomerID                                  AS RMSCustomerID,
    T.TransactionNumber,
    T.Time                                        AS SaleDate,
    CAST(T.Total AS decimal(14,2))                 AS GrossTotal,
    CAST(ISNULL(T.SalesTax, 0) AS decimal(14,2))  AS SalesTax,
    CAST(T.Total - ISNULL(T.SalesTax, 0) AS decimal(14,2)) AS PretaxAmount
FROM PUBLIC_Transaction T
WHERE
    T.CustomerID IS NOT NULL
    AND T.CustomerID <> 0
    AND T.Time >= DATEADD(day, -91, GETDATE())
    AND T.Time <= GETDATE()
ORDER BY
    T.CustomerID,
    T.Time,
    T.TransactionNumber;
"@

switch ($Action) {
  "test" {
    $row = Invoke-SqlRows "SELECT DB_NAME() AS DatabaseName, @@SERVERNAME AS ServerName, @@VERSION AS SqlVersion;"
    Write-Host "SQL connection succeeded." -ForegroundColor Green
    $row | Format-List
    $health = Invoke-RestMethod -Method Get -Uri "$($config.apiBaseUrl.TrimEnd('/'))/health"
    Write-Host "Railway API connection succeeded." -ForegroundColor Green
    $health | Format-List
  }

  "schema" {
    $q = @"
SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME IN ('Customer','PUBLIC_Transaction')
ORDER BY TABLE_NAME, ORDINAL_POSITION;
"@
    Invoke-SqlRows $q | Format-Table -AutoSize
  }

  "customers-preview" {
    $previewQuery = $customerQuery.Replace("ORDER BY C.ID;","ORDER BY C.ID OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY;")
    Invoke-SqlRows $previewQuery | Format-Table -AutoSize
  }

  "snapshot" {
    $customers = @(Invoke-SqlRows $customerQuery)
    $transactions = @(Invoke-SqlRows $transactionQuery)
    Write-Host "Customers read: $($customers.Count)"
    Write-Host "91-day transactions read: $($transactions.Count)"
    $batchSize = if ($config.batchSize) { [int]$config.batchSize } else { 250 }

    for ($i = 0; $i -lt $customers.Count; $i += $batchSize) {
      $end = [Math]::Min($i + $batchSize - 1, $customers.Count - 1)
      $batch = @($customers[$i..$end])
      $result = Invoke-BridgePost @{ customers = $batch; transactions = @() }
      Write-Host "Customer batch $($i + 1)-$($end + 1): staged $($result.customers_staged)"
    }

    for ($i = 0; $i -lt $transactions.Count; $i += $batchSize) {
      $end = [Math]::Min($i + $batchSize - 1, $transactions.Count - 1)
      $batch = @($transactions[$i..$end])
      $result = Invoke-BridgePost @{ customers = @(); transactions = $batch }
      Write-Host "Transaction batch $($i + 1)-$($end + 1): staged $($result.transactions_staged), resolved $($result.transactions_resolved)"
    }
    Write-Host "Snapshot upload complete." -ForegroundColor Green
  }
}
