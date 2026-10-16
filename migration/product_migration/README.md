# Product Migration (RMS -> Lightspeed via Colab)

This folder contains the product-migration pipeline for the Hobby Corner RMS-to-Lightspeed cutover.

## Current notebook

- Notebook version: `2026-10-16-v40`
- Branch: `product-migration-colab`
- Preferred RMS source: `/content/drive/MyDrive/Hobby Corner Migration/HCDB Test Items.csv`
- Legacy RMS candidate Google Sheet: fallback only when the Drive export is unavailable.
- Operational mapping workbook: `LS: importer`
- Dedicated review workbook: `Flagged Migration Items`

## Ground rules

- RMS on-hand quantity is **never** imported as opening Lightspeed inventory.
- Opening inventory comes from the full physical inventory performed at cutover.
- RMS quantity is used only as a migration-selection signal.
- Lightspeed supplier/category destinations come from the current `LS: importer` mapping/UUID data and retained supplier list, not from guessing raw RMS names.
- Unmapped or ambiguous category, supplier, brand, identifier, or source-identity values go to `REVIEW`; they are not allowed into a controlled write batch.
- `General > No Category` is only a visible review fallback label for an unmapped category. It does not count as an approved category mapping.
- Blank RMS primary suppliers remain unresolved/review even when the review sheet proposes `Other`.
- Named suppliers are mapped to `Other` only when that mapping is explicitly approved; stale blank-disposition `Other` rows are not trusted.
- Exact retained/current supplier names are preferred over stale `Other` mappings. Missing supplier UUIDs are resolved read-only from Lightspeed by exact name and cached when found.
- A nonblank RMS brand must resolve to the Brand ID Map before the product becomes write-eligible.
- Inactive items with RMS quantity > 0 remain migration candidates, but reorder values are forced to 0.
- DoNotOrder items have reorder values forced to 0.
- Default reorder suppression starts after 12 months without a reliable sale.
- Games Workshop items receive special review because part numbers may be reused.
- Source rows whose RMS description conflicts with the legacy web description, or whose valid barcode identity conflicts with the item code, are forced to review.
- The pipeline supports dry-run, controlled test batches, restart/resume, status logging, a permanent run history, and a failure review queue.
- Controlled product-write and reorder helpers enforce an absolute hard cap of 20 writes per invocation.
- Controlled product writes reject any payload containing outlet/on-hand inventory fields.
- Cleanup will not patch an existing Lightspeed test product from a source row that is currently identity-conflicted.
- Bulk processing will run in Google Colab Pro; GitHub remains the source of truth.

## Mapping behavior

### Category

`Mapping` is the RMS Department + RMS Category -> Lightspeed category-path source. `Cat ID` supplies the Lightspeed UUID for the exact resolved path.

A category is considered mapped only when there is an explicit nonblank RMS mapping. An unmapped source classification is shown as `General > No Category` for review visibility, but remains `Category_Mapped=False` and cannot be selected for a write batch.

Legacy/system classifications intentionally excluded from migration remain `EXCLUDE` rather than creating mapping work.

### Supplier

The primary RMS supplier is `Item.SupplierID`; secondary SupplierList relationships are not used to choose the primary supplier.

Supplier resolution order is:

1. explicit approved RMS -> Lightspeed supplier mapping;
2. exact current name in `Supplier ID Map`;
3. exact retained name in the `Supplier` template, followed by exact read-only Lightspeed UUID resolution;
4. otherwise `REVIEW`.

Blank source suppliers and unresolved named suppliers are never silently changed to `Other`.

For supplier cost, the payload prefers the cost from the SupplierList row for the RMS primary supplier (`RMS_SupplierListCost`), then falls back to item-level cost fields.

### Brand

A blank RMS brand is allowed. A nonblank brand must resolve exactly in `Brand ID Map`; otherwise the row is `REVIEW`.

### Product identity

The family name prefers `RMS_WebDescription` and falls back to `RMS_Description`, but source-identity guards run before a row can become write-eligible. Obviously unrelated web-description text and conflicting valid barcode identity are review conditions.

SKU remains a CUSTOM product code. RMS UPC and aliases are preserved as product codes according to their source/code shape.

## Controlled create verification

After a controlled product-family create, the writer reads the created family/product back from Lightspeed and verifies:

- family name
- long description
- category UUID
- brand UUID
- SKU/UPC/alias codes
- supplier UUID
- supplier item code
- supplier price
- retail price
- weight and weight unit
- active flags
- family image when an image was requested

Any field mismatch changes the result to `ERROR` and records the mismatch rather than treating the create as verified.

Duplicate protection currently gates on exact SKU before create. Source identity checks provide additional barcode protection, but full-catalog preflight against every existing Lightspeed code remains a recommended final-cutover safeguard.

## Reorder behavior

Lightspeed reorder configuration uses `MIN_MAX`:

- proposed RMS reorder point -> Lightspeed minimum/reorder point
- proposed RMS restock level -> Lightspeed target maximum

The reorder helper reads current inventory immediately before and after the update and raises a safety failure if on-hand changes.

## Review outputs

The migration writes/maintains:

- `LS: importer / Migration Review` — compatibility review view.
- `LS: importer / Migration Unmapped Categories` — ranked category gaps.
- `LS: importer / Migration Unmapped Suppliers` — ranked supplier gaps.
- `LS: importer / Migration Failure Review` — failed controlled writes.
- `Flagged Migration Items / Flagged Items` — dedicated durable review queue with mapping/identity reasons and manual review fields.

Manual review fields in the dedicated flagged workbook are preserved when the current review queue is synchronized.

## Planned flow

1. Load the preferred RMS Drive export.
2. Load current `LS: importer` category, supplier and UUID data.
3. Normalize source fields.
4. Apply migration/reorder/source-identity rules.
5. Resolve category/supplier/brand eligibility.
6. Send unresolved or ambiguous rows to `REVIEW`.
7. Build a varied, fully mapped dry-run preview.
8. Run a bounded controlled live batch of at most 20 products and perform full read-back verification.
9. Review test results, flagged items, images, and reorder behavior.
10. Delete controlled test products before final cutover when explicitly authorized.
11. Run the full migration with resumable batching after final approval.
12. Perform physical inventory in Lightspeed to establish on-hand quantities.

## Status values

- READY
- REVIEW
- EXCLUDE
- CREATED
- UPDATED
- VERIFIED
- SKIPPED_EXISTING
- ERROR

## Current policy decisions still open

- Inactive RMS items with quantity > 0 are migration candidates, but controlled payloads currently set `active.in_store=True`. Decide whether final cutover should preserve RMS inactive state in Lightspeed.
- Controlled payloads currently set `active.ecwid=False`. Online-channel activation is a separate cutover decision.
- Tags are not currently part of the RMS migration payload.
- Full-catalog duplicate/code-collision preflight beyond exact SKU should be completed before final catalog cutover.

## Notes

Category/supplier mappings can change while migration cleanup continues. The pipeline must always read the latest live mapping data at execution time.

No production Railway deployment is required for the Colab migration notebook.
