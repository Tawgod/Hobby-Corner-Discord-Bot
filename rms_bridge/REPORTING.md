# RMS reporting companion (opt-in; migration-safe)

This is a **new** PowerShell script. The existing `bridge.ps1`, `install.ps1`, migration snapshot, rewards API calls and `config.example.json` remain unchanged.

## Usage (from RMS server)
Copy `reporting.ps1` beside the installed `bridge.ps1` and `config.json`.
Run only after examining its SQL and output destination:

```powershell
powershell -ExecutionPolicy Bypass -File .\reporting.ps1 -Action schema-check
powershell -ExecutionPolicy Bypass -File .\reporting.ps1 -Action inventory -OutputDir C:\HobbyCorner\RMSBridge\reports
```

`schema-check` reads metadata only. `inventory` reads dbo.Item, dbo.Department and dbo.Category, then writes a locally stored CSV. It is not scheduled, does not upload anything, and does not change the database. The query fails closed if required schema columns are missing.

**Important:** A live RMS schema/query validation has not yet occurred. Cost uses `Item.Cost` and retail uses `Item.Price` as a provisional valuation; compare the results with RMS Item Value List before relying on them, especially if historical report valuation differs. Supplier, brand, and precise sale totals are deliberately deferred until tables and definitions can be verified.

`sales` is blocked until line-level sales, returns, voids, and discounts are reconciled. The legacy rewards bridge's 91-day customer-only transaction sample is not a full store-sales export.

Security: CSVs may contain sensitive commercial information. Store them in a restricted directory. `reporting.ps1` uses the existing bridge's SQL configuration and does not transmit credentials or query results. No automatic job or Railway deployment is included.
