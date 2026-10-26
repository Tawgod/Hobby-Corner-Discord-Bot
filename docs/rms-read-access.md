# RMS / HQ read-access map

## Purpose

This branch expands the existing Hobby Corner RMS bridge into a reusable, read-only operational/history access layer. It is intentionally separate from the product-migration branch so the known-good product export remains unchanged.

The bridge does **not** accept arbitrary SQL. Remote requests are limited to named actions, named query presets, and exact-table reads resolved from database metadata. Every SQL statement executed by `Invoke-SqlRows` passes a local read-only SQL guard before it is sent to SQL Server.

## Safety model

- SQL connection continues to use the dedicated `hobbycorner_read` login.
- The bridge rejects SQL that does not begin with `SELECT` or `WITH`.
- The bridge rejects write/admin keywords including `INSERT`, `UPDATE`, `DELETE`, `MERGE`, DDL, permission changes, `EXEC`, backup/restore, and `SELECT ... INTO`.
- The Windows polling worker has its own action allowlist in addition to the API allowlist.
- Named historical queries use SQL parameters for user-supplied values.
- Query result limits are capped at 2,000 rows.
- Table preview remains metadata-resolved and capped at 100 rows.
- No destructive or RMS write operation is implemented.

The database login should still remain SQL-side read-only. Application safeguards are defense in depth, not a replacement for SQL permissions.

## General HCDB / HQ explorer

For future migration, reporting, reconciliation, or business-history work, the bridge can now enumerate and read exact tables/views without adding a new query each time.

- `source-tables -DataSource store` lists HCDB tables/views.
- `source-schema -DataSource store -TableName <name>` returns a table's columns.
- `table-read -DataSource store -TableName <name> -Limit <1..2000>` returns bounded JSON rows.
- The same actions support `-DataSource hq`, using `hqDatabase` from config (default `HQcorp`).

This is the broad access path for customer, sales, account/AR, item, item movement, inventory, purchase-order, supplier, department/category, and other legacy HCDB data as those tables are identified. HQ should primarily be used for web/item metadata.

Generic reads accept an exact table/view name only; they do not accept caller-supplied SQL or arbitrary WHERE clauses. Common datasets should still graduate into named presets once their joins/keys are confirmed.

## Existing confirmed relationships

These are already used by the working product/customer migration queries on `product-migration-colab`:

| Source | Relationship | Use |
|---|---|---|
| `Item.DepartmentID` | -> `Department.ID` | Item department |
| `Item.CategoryID` | -> `Category.ID` | Item category |
| `Item.SupplierID` | -> `Supplier.ID` | Primary item supplier |
| `SupplierList.ItemID` | -> `Item.ID` | Supplier-item relationship |
| `SupplierList.SupplierID` | -> `Supplier.ID` | Supplier-item relationship |
| `Alias.ItemID` | -> `Item.ID` | UPC / alternate item codes |
| `Item.HQID` | -> `HQcorp.dbo.nitroasl_pamtable.ItemID` | HQ web description; this is the corrected HQ join used by product migration |
| `PUBLIC_Transaction.CustomerID` | -> `Customer.ID` | Customer purchase/rewards history |

The product export query itself was not changed by this branch.

## Historical / operational sources

### Ready with fields already used by current migration code

- `PUBLIC_Transaction`
  - receipt / transaction header
  - confirmed fields in current bridge: `StoreID`, `BatchNumber`, `TransactionNumber`, `Time`, `CustomerID`, `Total`, `SalesTax`
- `Customer`
  - customer master plus summary fields such as total sales, visits, account opened, last visit and last updated
- `Item`
  - current SKU, description, department/category/supplier IDs, current quantity, cost/last cost/replacement cost, retail/MSRP, last sold/received and last updated
- `Supplier`, `SupplierList`
  - supplier master and supplier-item reorder number/cost
- `Department`, `Category`
  - current classification
- `Alias`
  - UPC / alternate item identifiers
- `HQcorp.dbo.nitroasl_pamtable`
  - HQ product web description via `Item.HQID`

### Runtime-schema-checked read

- `PUBLIC_TransactionEntry`
  - intended item-level sale-line source
  - the `sales-by-item` preset requires `StoreID`, `TransactionNumber`, `ItemID`, `Quantity`, and `Price`
  - it joins to `PUBLIC_Transaction` by `StoreID + TransactionNumber`
  - the bridge refuses to run the preset if any required table/column is absent

### Discovery targets requiring live schema confirmation

The `operational-discovery` action inventories columns and row counts for likely sources without changing them:

- `Transaction`, `TransactionEntry`, `PUBLIC_TransactionEntry`
- `PurchaseOrder`, `PurchaseOrderEntry`, `PurchaseOrderEntryDetail`
- `InventoryTransfer`, `InventoryTransferEntry`, `InventoryTransferLog`, `InventoryMovement`, `ItemMovement`
- `ItemDynamic`
- `AccountReceivable`, `AccountReceivableHistory`, `ARHistoryMirror`
- `Voucher`, `VoucherEntry`, `VoucherFormatNumber`
- NitroSell / WebLinK product, category, navigation and image tables
- local `nitroasl_pamtable`

These candidates cover purchase-order history, inventory movement/history, receivables, vouchers/store-credit history, legacy web/category history, and other useful migration evidence. We should not hard-code joins for them until the live RMS/HQ columns are captured.

## Named read presets

`bridge.ps1 -Action read-catalog` lists the allowed presets.

### `receipts`

Receipt header / customer purchase history from `PUBLIC_Transaction`.

Optional filters:
- `CustomerID`
- `StartDate`
- `EndDate` (exclusive)

### `sales-by-item`

Historical item/SKU sale lines from `PUBLIC_TransactionEntry + PUBLIC_Transaction + Item`.

Optional filters:
- `ItemLookupCode`
- `StartDate`
- `EndDate` (exclusive)

The required transaction-entry columns are verified against `INFORMATION_SCHEMA.COLUMNS` before the query executes.

### `supplier-items`

All supplier-item relationships, supplier reorder numbers and supplier-list costs from `SupplierList + Supplier + Item`.

Optional filter:
- `SupplierID`

### `item-pricing`

Current RMS price/cost snapshot from `Item`:
- Cost
- LastCost
- ReplacementCost
- Retail Price
- MSRP
- LastUpdated

Optional filter:
- `ItemLookupCode`

This is a current snapshot, not yet a historical price-change log.

### `classifications`

Current item -> department/category mapping.

### `customers`

Customer master and historical summary fields currently present on the customer record.

## Remote API / worker flow

The existing outbound-polling architecture is preserved:

1. Admin API queues a named RMS job.
2. RMS Windows worker polls for the job.
3. Worker accepts only allowlisted bridge actions.
4. For `read-query`, the worker forwards only validated named parameters to `bridge.ps1`.
5. `bridge.ps1` selects a predefined query, validates required columns, applies parameterized filters, enforces the row cap, and executes through the read-only SQL guard.
6. Result text is returned to the existing RMS job result store.

No inbound SQL connection is opened.

## Next live discovery pass

Run these from the RMS bridge after the updated worker is installed:

```powershell
.\bridge.ps1 -Action safety-test
.\bridge.ps1 -Action read-catalog
.\bridge.ps1 -Action operational-discovery
.\bridge.ps1 -Action read-query -QueryName receipts -Limit 10
.\bridge.ps1 -Action read-query -QueryName sales-by-item -Limit 10
.\bridge.ps1 -Action read-query -QueryName supplier-items -Limit 10
.\bridge.ps1 -Action read-query -QueryName item-pricing -Limit 10
.\bridge.ps1 -Action read-query -QueryName classifications -Limit 10
.\bridge.ps1 -Action read-query -QueryName customers -Limit 10
```

After the operational discovery output confirms the purchase-order and inventory movement schemas, add those joins as new named presets rather than enabling arbitrary SQL.

## October 26 live HCDB schema evidence

The store's Windows PowerShell read tests confirmed **230 HCDB tables/views**, **16 HQcorp tables/views**, working receipt/supplier query results, and an updated worker task pointing to `C:\HobbyCorner\RMSBridge\worker.ps1`.

The following are now grounded in actual HCDB columns (the table names and column types were supplied by the RMS machine; joins must still be validated with live sample rows):

| Read preset | Table(s) | Linking/date fields | Caution |
|---|---|---|---|
| `item-movements` | `InventoryTransferLog`, `Item` | `ItemID -> Item.ID`, `DateTransferred` | `Type`/sign semantics need verification |
| `item-value-history` | `ItemValueLog`, `Item` | `ItemID -> Item.ID`, `LastUpdated` | `AmountType` meanings need verification |
| `physical-inventory` | `PhysicalInventory`, `PhysicalInventoryEntry`, `Item` | `PhysicalInventoryID -> ID`, `StoreID`, `ItemID`, `OpenTime` | Physical count records are not equivalent to cutover stock |
| `account-history` | `AccountReceivableHistory` | `AccountReceivableID`, `Date`, `PaymentID`, `HistoryType` | Need account master schema to map to customers |
| `purchase-order-lines` | `PurchaseOrderEntry`, `Item` | `PurchaseOrderID`, `ItemID`, `LastUpdated`, `LastReceivedDate` | Need PurchaseOrder header schema for supplier/order date |
| `order-history` | `OrderHistory` | `OrderID`, `Date`, `TransactionNumber` | Need Order master schema for customer linkage |

Also confirmed: `TransactionEntry` includes `StoreID`, `TransactionNumber`, `ItemID`, `Price`, `Cost`, `Quantity`, `SalesTax`, and `TransactionTime`; the sale-line join must be verified for `PUBLIC_TransactionEntry` and transaction batch/store identity before reporting totals.

The existing `VIEWITEMMOVEMENTHISTORY` view contains `ITEMLOOKUPCODE`, `TYPE`, `QUANTITY`, `DATETRANSFERRED`, `REFERENCEID`, `REFERENCEENTRYID`, `PONUMBER` and `TRANSACTIONNUMBER`. `VIEWITEMMOVEMENT` adds price and cost sold. These views are useful for consistency checks.

### Next schema reads

Collect field definitions from `Transaction`, `PUBLIC_TransactionEntry`, `PurchaseOrder`, `AccountReceivable`, `Order`, and `Item`, then test **five non-sensitive rows** from each new preset. Confirm movement type and value-log amount-type semantics before calculating inventory or pricing trends.
