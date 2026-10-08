param(
  [ValidateSet("test","schema","product-schema","database-schema","database-tables","database-relations","table-preview","table-count","customers-preview","product-export","snapshot")]
  [string]$Action = "test",
  [string]$ConfigPath = "$PSScriptRoot\config.json",
  [string]$TableName = "",
  [int]$PreviewRows = 25,
  [string]$ProductExportPath = "$PSScriptRoot\exports\HCDB Test Items.csv"
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

function Resolve-SafeTableName([string]$RequestedTable) {
  $name = [string]$RequestedTable
  if ([string]::IsNullOrWhiteSpace($name)) { throw "TableName is required." }
  $escaped = $name.Replace("'", "''")
  $rows = @(Invoke-SqlRows "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = '$escaped';")
  if ($rows.Count -eq 0) { throw "Unknown RMS table or view: $name" }
  if ($rows.Count -gt 1) { throw "Table name is ambiguous across schemas: $name" }
  $schema = [string]$rows[0].TABLE_SCHEMA
  $table = [string]$rows[0].TABLE_NAME
  return "[" + $schema.Replace("]", "]]") + "].[" + $table.Replace("]", "]]") + "]"
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
    C.AccountNumber,
    C.Employee,
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

$productExportQuery = @"
SELECT
    I.ID                                      AS RMS_ItemID,
    I.ItemLookupCode                          AS RMS_SKU,
    I.Description                             AS RMS_Description,
    I.DepartmentID                            AS RMS_DepartmentID,
    D.Name                                    AS RMS_Department,
    D.Code                                    AS RMS_DepartmentCode,
    I.CategoryID                              AS RMS_CategoryID,
    C.Name                                    AS RMS_Category,
    C.Code                                    AS RMS_CategoryCode,
    I.SupplierID                              AS RMS_SupplierID,
    S.SupplierName                            AS RMS_Supplier,
    S.Code                                    AS RMS_SupplierCode,
    PSL.ReorderNumber                         AS RMS_SupplierItemCode,
    PSL.Cost                                  AS RMS_SupplierListCost,
    I.Cost,
    I.LastCost,
    I.ReplacementCost,
    I.Price                                   AS RetailPrice,
    I.MSRP,
    I.Quantity                                AS RMS_Quantity,
    I.ReorderPoint                            AS RMS_ReorderPoint,
    I.RestockLevel                            AS RMS_RestockLevel,
    I.LastSold,
    I.LastReceived,
    I.DateCreated,
    I.LastUpdated,
    I.Inactive,
    I.DoNotOrder,
    I.ExtendedDescription,
    I.SubDescription1,
    I.SubDescription2,
    I.SubDescription3                         AS Brand,
    I.SubDescription3,
    I.PictureName,
    I.Weight,
    BAR.PrimaryBarcode                        AS RMS_UPC,
    ALS.AllAliases                            AS RMS_Aliases
FROM Item I
LEFT JOIN Department D ON D.ID = I.DepartmentID
LEFT JOIN Category C ON C.ID = I.CategoryID
LEFT JOIN Supplier S ON S.ID = I.SupplierID
OUTER APPLY (
    SELECT TOP 1
        SL.ReorderNumber,
        SL.Cost
    FROM SupplierList SL
    WHERE SL.ItemID = I.ID
      AND SL.SupplierID = I.SupplierID
    ORDER BY SL.ID
) PSL
OUTER APPLY (
    SELECT TOP 1 A.Alias AS PrimaryBarcode
    FROM Alias A
    WHERE A.ItemID = I.ID
      AND A.Alias IS NOT NULL
      AND LTRIM(RTRIM(A.Alias)) <> ''
      AND A.Alias NOT LIKE '%[^0-9]%'
      AND LEN(LTRIM(RTRIM(A.Alias))) IN (8, 12, 13, 14)
    ORDER BY
      CASE LEN(LTRIM(RTRIM(A.Alias)))
        WHEN 12 THEN 1
        WHEN 13 THEN 2
        WHEN 14 THEN 3
        WHEN 8 THEN 4
        ELSE 9
      END,
      A.ID
) BAR
OUTER APPLY (
    SELECT STUFF((
        SELECT '|' + REPLACE(LTRIM(RTRIM(A2.Alias)), '|', '')
        FROM Alias A2
        WHERE A2.ItemID = I.ID
          AND A2.Alias IS NOT NULL
          AND LTRIM(RTRIM(A2.Alias)) <> ''
        ORDER BY A2.ID
        FOR XML PATH(''), TYPE
    ).value('.', 'nvarchar(max)'), 1, 1, '') AS AllAliases
) ALS
ORDER BY I.ID;
"@

$transactionQuery = @"
SELECT
    T.CustomerID                                  AS RMSCustomerID,
    T.StoreID,
    T.BatchNumber,
    T.TransactionNumber,
    T.Time                                        AS SaleDate,
    CAST(T.Total AS decimal(14,2))                 AS GrossTotal,
    CAST(ISNULL(T.SalesTax, 0) AS decimal(14,2))  AS SalesTax,
    CAST(T.Total - ISNULL(T.SalesTax, 0) AS decimal(14,2)) AS PretaxAmount
FROM PUBLIC_Transaction T
JOIN Customer C ON C.ID = T.CustomerID
WHERE
    T.CustomerID IS NOT NULL
    AND T.CustomerID <> 0
    AND T.Time >= DATEADD(day, -91, GETDATE())
    AND T.Time <= GETDATE()
    AND ISNULL(C.Employee, 0) <> 1
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

  "product-schema" {
    $q = @"
SELECT
    TABLE_NAME,
    COLUMN_NAME,
    DATA_TYPE,
    CHARACTER_MAXIMUM_LENGTH
FROM INFORMATION_SCHEMA.COLUMNS
WHERE
    TABLE_NAME LIKE '%Item%'
    OR TABLE_NAME LIKE '%Product%'
    OR TABLE_NAME LIKE '%Inventory%'
    OR TABLE_NAME LIKE '%Supplier%'
    OR TABLE_NAME LIKE '%Department%'
    OR TABLE_NAME LIKE '%Category%'
    OR TABLE_NAME LIKE '%Purchase%'
    OR TABLE_NAME LIKE '%Transaction%'
    OR COLUMN_NAME LIKE '%Reorder%'
    OR COLUMN_NAME LIKE '%Restock%'
    OR COLUMN_NAME LIKE '%Quantity%'
    OR COLUMN_NAME LIKE '%Qty%'
    OR COLUMN_NAME LIKE '%OnHand%'
    OR COLUMN_NAME LIKE '%OnOrder%'
    OR COLUMN_NAME LIKE '%Supplier%'
    OR COLUMN_NAME LIKE '%Department%'
    OR COLUMN_NAME LIKE '%Category%'
    OR COLUMN_NAME LIKE '%Discontinued%'
    OR COLUMN_NAME LIKE '%Inactive%'
    OR COLUMN_NAME LIKE '%LastSold%'
    OR COLUMN_NAME LIKE '%LastSale%'
    OR COLUMN_NAME LIKE '%LastReceived%'
    OR COLUMN_NAME LIKE '%LastPurchase%'
    OR COLUMN_NAME LIKE '%Cost%'
    OR COLUMN_NAME LIKE '%Price%'
ORDER BY TABLE_NAME, ORDINAL_POSITION;
"@
    $rows = @(Invoke-SqlRows $q)
    Write-Host "Product/inventory schema candidates: $($rows.Count)" -ForegroundColor Green
    $rows | Format-Table -AutoSize
  }

  "database-schema" {
    $q = @"
SELECT
    T.TABLE_SCHEMA,
    T.TABLE_NAME,
    T.TABLE_TYPE,
    C.ORDINAL_POSITION,
    C.COLUMN_NAME,
    C.DATA_TYPE,
    C.CHARACTER_MAXIMUM_LENGTH,
    C.NUMERIC_PRECISION,
    C.NUMERIC_SCALE,
    C.IS_NULLABLE
FROM INFORMATION_SCHEMA.TABLES T
LEFT JOIN INFORMATION_SCHEMA.COLUMNS C
  ON C.TABLE_SCHEMA=T.TABLE_SCHEMA AND C.TABLE_NAME=T.TABLE_NAME
ORDER BY T.TABLE_SCHEMA,T.TABLE_NAME,C.ORDINAL_POSITION;
"@
    $rows=@(Invoke-SqlRows $q)
    Write-Host "Database schema rows: $($rows.Count)" -ForegroundColor Green
    $rows | Format-Table -AutoSize
  }

  "database-tables" {
    $q = @"
SELECT
    TABLE_SCHEMA,
    TABLE_NAME,
    TABLE_TYPE
FROM INFORMATION_SCHEMA.TABLES
ORDER BY TABLE_SCHEMA,TABLE_NAME;
"@
    $rows=@(Invoke-SqlRows $q)
    Write-Host "Tables/views found: $($rows.Count)" -ForegroundColor Green
    $rows | Format-Table -AutoSize
  }

  "database-relations" {
    $q = @"
SELECT
    fk.name AS ForeignKeyName,
    OBJECT_SCHEMA_NAME(fk.parent_object_id) AS ChildSchema,
    OBJECT_NAME(fk.parent_object_id) AS ChildTable,
    COL_NAME(fkc.parent_object_id,fkc.parent_column_id) AS ChildColumn,
    OBJECT_SCHEMA_NAME(fk.referenced_object_id) AS ParentSchema,
    OBJECT_NAME(fk.referenced_object_id) AS ParentTable,
    COL_NAME(fkc.referenced_object_id,fkc.referenced_column_id) AS ParentColumn
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id=fk.object_id
ORDER BY ChildSchema,ChildTable,ForeignKeyName,fkc.constraint_column_id;
"@
    $rows=@(Invoke-SqlRows $q)
    Write-Host "Foreign-key relationships found: $($rows.Count)" -ForegroundColor Green
    if ($rows.Count -eq 0) { Write-Host "No declared foreign keys were found. RMS may rely on implicit relationships." }
    else { $rows | Format-Table -AutoSize }
  }

  "table-count" {
    $safeTable=Resolve-SafeTableName $TableName
    $rows=Invoke-SqlRows "SELECT COUNT_BIG(*) AS RowCount FROM $safeTable;"
    Write-Host "Table: $TableName" -ForegroundColor Green
    $rows | Format-List
  }

  "table-preview" {
    if ($PreviewRows -lt 1) { $PreviewRows=25 }
    if ($PreviewRows -gt 100) { $PreviewRows=100 }
    $safeTable=Resolve-SafeTableName $TableName
    Write-Host "Previewing TOP $PreviewRows rows from $TableName" -ForegroundColor Green
    Invoke-SqlRows "SELECT TOP $PreviewRows * FROM $safeTable;" | Format-Table -AutoSize
  }

  "product-export" {
    $exportDir = Split-Path -Parent $ProductExportPath
    if (-not (Test-Path $exportDir)) {
      New-Item -ItemType Directory -Path $exportDir -Force | Out-Null
    }

    Write-Host "Reading enriched RMS product catalog..." -ForegroundColor Cyan
    $rows = @(Invoke-SqlRows $productExportQuery)
    Write-Host "Products read: $($rows.Count)" -ForegroundColor Green

    $rows | Export-Csv -Path $ProductExportPath -NoTypeInformation -Encoding UTF8
    $file = Get-Item $ProductExportPath
    Write-Host "Product export complete." -ForegroundColor Green
    Write-Host "Path: $($file.FullName)"
    Write-Host "Size: $([math]::Round($file.Length / 1MB, 2)) MB"
    Write-Host "Brand source: Item.SubDescription3"
    Write-Host "UPC/aliases source: Alias"
    Write-Host "Supplier item code source: SupplierList.ReorderNumber"
    Write-Host "Supplier code source: Supplier.Code"
  }

  "customers-preview" {
    $previewQuery = $customerQuery.Replace("SELECT`n    C.ID", "SELECT TOP 10`n    C.ID")
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
