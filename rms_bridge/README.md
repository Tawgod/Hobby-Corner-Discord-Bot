# Hobby Corner RMS Bridge

Windows-side, read-only bridge for the former Microsoft RMS / SQL Server databases.

The bridge reads RMS locally with the dedicated `hobbycorner_read` login and can return bounded read results through the existing outbound-polling admin job system. It never writes to RMS/HQ.

## Data sources

- `store` — the configured store database (HCDB in production). This is the primary historical source for customer, sales, account/AR, item, purchasing, item-movement and inventory data.
- `hq` — the configured HQ database, default `HQcorp`. This is primarily useful for legacy web/item metadata.

Existing product-migration queries are preserved. In particular, the working product export still joins `HQcorp.dbo.nitroasl_pamtable` by `Item.HQID`.

## Safety

The bridge uses defense in depth:

1. dedicated SQL login `hobbycorner_read` should have SELECT-only database permissions;
2. all SQL executed through `Invoke-SqlRows` must pass the local SELECT/WITH-only guard;
3. write/admin SQL keywords and `SELECT ... INTO` are rejected;
4. the Railway admin API accepts only named RMS job types;
5. the Windows worker has a separate allowlist;
6. arbitrary SQL text is never accepted from the API;
7. generic table reads require an exact table/view name resolved from database metadata;
8. table reads are capped at 2,000 rows per request and previews at 100.

## Setup

1. Copy this folder to the RMS server.
2. Run PowerShell as Administrator.
3. Run `.\install.ps1`.
4. Edit `C:\HobbyCorner\RMSBridge\config.json`.
5. Set:
   - store/RMS SQL server
   - HCDB database name
   - HQ database name (normally `HQcorp`)
   - `hobbycorner_read` password
   - Rewards/admin API key
6. Test:
   `powershell -ExecutionPolicy Bypass -File C:\HobbyCorner\RMSBridge\bridge.ps1 -Action test`
7. Run the local safety regression:
   `powershell -ExecutionPolicy Bypass -File C:\HobbyCorner\RMSBridge\bridge.ps1 -Action safety-test`

## General read access

List all HCDB tables/views:

```powershell
.\bridge.ps1 -Action source-tables -DataSource store
```

List all HQ tables/views:

```powershell
.\bridge.ps1 -Action source-tables -DataSource hq
```

Inspect one exact table schema:

```powershell
.\bridge.ps1 -Action source-schema -DataSource store -TableName PUBLIC_TransactionEntry
```

Read up to 2,000 rows from any exact table/view:

```powershell
.\bridge.ps1 -Action table-read -DataSource store -TableName Item -Limit 250
.\bridge.ps1 -Action table-read -DataSource hq -TableName nitroasl_pamtable -Limit 250
```

These generic reads do not accept arbitrary WHERE clauses or SQL. For commonly used datasets, prefer the named query presets because they provide parameterized filters and stable columns.

## Named read presets

Show the catalog:

```powershell
.\bridge.ps1 -Action read-catalog
```

Examples:

```powershell
.\bridge.ps1 -Action read-query -QueryName receipts -StartDate 2025-01-01 -EndDate 2026-01-01 -Limit 500
.\bridge.ps1 -Action read-query -QueryName sales-by-item -ItemLookupCode GW48-111 -Limit 500
.\bridge.ps1 -Action read-query -QueryName supplier-items -SupplierID 12 -Limit 500
.\bridge.ps1 -Action read-query -QueryName item-pricing -ItemLookupCode GW48-111
.\bridge.ps1 -Action read-query -QueryName classifications -Limit 500
.\bridge.ps1 -Action read-query -QueryName customers -Limit 500
```

## Discovery

`-Action operational-discovery` inventories likely historical sources for:

- transaction headers and line items
- purchase orders
- inventory transfers/movement
- item dynamic/history data
- customers and account receivable history
- vouchers
- legacy web/category/navigation/image data

The discovered schema should be documented in `docs/rms-read-access.md` before adding a stable named join/query.

## Existing migration actions

Existing customer snapshot, product export and migration discovery actions remain available. `snapshot` still posts customer/rewards migration data to the current Rewards API and is not a write to RMS itself.

The product-migration export query is intentionally preserved; do not change it as part of general historical access work unless the product migration itself requires a reviewed fix.
