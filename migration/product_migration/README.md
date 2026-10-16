# Product Migration (RMS -> Lightspeed via Colab)

This folder contains the product-migration pipeline for the Hobby Corner RMS-to-Lightspeed cutover.

## Current notebook

- Notebook version: `2026-10-16-v42`
- Branch: `product-migration-colab`
- Authoritative RMS source: `/content/drive/MyDrive/Hobby Corner Migration/HCDB Test Items.csv`
- Legacy RMS candidate Google Sheet: emergency fallback only; disabled by default with `ALLOW_LEGACY_SOURCE_FALLBACK = False`.
- Operational mapping workbook: `LS: importer`
- Dedicated review workbook: `Flagged Migration Items`

## Ground rules

- RMS on-hand quantity is **never** imported as opening Lightspeed inventory.
- Opening inventory comes from the full physical inventory performed at cutover.
- RMS quantity is used only as a migration-selection signal.
- The notebook fails closed when the authoritative Drive CSV is missing unless legacy fallback is deliberately enabled.
- The current RMS bridge schema, required item ID/SKU/description values, and unique RMS Item IDs are validated before mapping analysis.
- Lightspeed supplier/category destinations come from the current `LS: importer` mappings/UUID data and retained supplier list, not from guessing raw RMS names.
- Conflicting duplicate category, supplier, brand, or UUID mapping rows stop setup instead of using a last-row-wins rule.
- Unmapped or ambiguous category, supplier, brand, identifier, or source-code values go to `REVIEW`; they are not allowed into a controlled write batch.
- `General > No Category` is a visible review label only. It does not count as an approved category mapping.
- Blank RMS primary suppliers remain unresolved/review even when a review row proposes `Other`.
- Named suppliers are mapped to `Other` only when that conversion is explicitly approved; stale blank-disposition `Other` rows are not trusted.
- Exact retained/current supplier names are preferred over stale `Other` mappings. Missing supplier UUIDs are resolved read-only from Lightspeed by exact name and cached when found.
- A nonblank RMS brand must resolve to `Brand ID Map` before the product becomes write-eligible.
- Inactive items with RMS quantity > 0 remain migration candidates, but reorder values are forced to 0.
- DoNotOrder items have reorder values forced to 0.
- Default reorder suppression starts after 12 months without a reliable sale.
- Games Workshop items receive special review because part numbers may be reused.
- Strong source-identity conflicts such as a valid barcode that conflicts with a GTIN-like item code are forced to review.
- Product-code collisions between migration candidates are detected across SKU, UPC and RMS aliases and forced to review before batch selection.
- The pipeline supports dry-run, controlled test batches, restart/resume, status logging, a permanent run history, and a failure review queue.
- Controlled product-write and reorder helpers enforce an absolute hard cap of 20 writes per invocation.
- Controlled product writes reject any payload containing outlet/on-hand inventory fields.
- Bulk processing will run in Google Colab Pro; GitHub remains the source of truth.

## Source validation

The current bridge export is expected to contain the full 38-column product schema produced by `rms_bridge/bridge.ps1`, including source IDs, RMS classification, primary supplier and SupplierList fields, cost/retail fields, activity/reorder fields, descriptions, brand, picture, weight, UPC and aliases.

Setup stops before mapping analysis when:

- the authoritative CSV is missing and legacy fallback is not explicitly enabled;
- required bridge columns are missing;
- RMS Item ID, SKU, or Description is blank;
- RMS Item ID is duplicated.

The fresh export reviewed on 2026-10-16 contains 54,255 product rows.

## Mapping behavior

### Category

`Mapping` is the RMS Department + RMS Category -> Lightspeed category-path source. `Cat ID` supplies the Lightspeed UUID for the exact resolved path.

A category is considered mapped only when there is an explicit nonblank RMS mapping **and** the resulting path has a `Cat ID` UUID. Missing category UUIDs are moved to review.

An unmapped source classification is shown as `General > No Category` for review visibility, but remains `Category_Mapped=False` and cannot be selected for a write batch.

Legacy/system classifications intentionally excluded from migration remain `EXCLUDE` rather than creating mapping work.

### Supplier

The primary RMS supplier is `Item.SupplierID`; secondary SupplierList relationships are not used to choose the primary supplier.

Supplier resolution order is:

1. explicit approved RMS -> Lightspeed supplier mapping;
2. exact current name in `Supplier ID Map`;
3. exact retained name in the `Supplier` template, followed by exact read-only Lightspeed UUID resolution;
4. otherwise `REVIEW`.

Blank source suppliers and unresolved named suppliers are never silently changed to `Other`.

For supplier cost, the payload uses the first **positive** value in this precedence:

1. `RMS_SupplierListCost` for the RMS primary supplier;
2. `LastCost`;
3. item `Cost`;
4. `ReplacementCost`;
5. compatible fallback cost fields.

Zero placeholders do not mask a later real cost. When RMS has no positive cost, supplier price is omitted and the controlled preview marks supply cost as missing.

### Brand

A blank RMS brand is allowed. A nonblank brand must resolve exactly in `Brand ID Map`; otherwise the row is `REVIEW`.

### Product identity and names

The migration uses `RMS_WebDescription` as the family name only when it can be corroborated against the RMS Description / ExtendedDescription. If a web title cannot be corroborated, it receives a non-blocking `web_description_untrusted` warning and the product name falls back to the RMS Description.

This avoids both failure modes seen during testing:

- blindly using stale NitroSell/PAM titles from another product; and
- blocking large numbers of legitimate products whose RMS descriptions are abbreviated.

Strong barcode identity conflicts remain blocking review conditions.

### Product codes

- RMS SKU is written as a `CUSTOM` product code.
- 12-digit numeric codes are typed as `UPC`.
- 8- and 13-digit numeric codes are typed as `EAN`.
- 14-digit numeric codes are typed as `ITF`.
- Other aliases remain `CUSTOM`.

Source-internal product-code collisions are detected before batch selection. The 2026-10-16 export audit found one duplicate SKU and six colliding code values among migration candidates; affected rows are routed to review.

## Controlled create verification

The exact-SKU pre-create duplicate lookup uses the current `/api/2026-10/products` endpoint.

After a controlled product-family create, the writer reads the created family/product back from Lightspeed and verifies:

- family name
- long description
- category UUID
- brand UUID
- SKU/UPC/EAN/ITF/alias codes
- supplier UUID
- supplier item code
- supplier price
- retail price
- weight and weight unit
- active flags
- family image when an image was requested

Any field mismatch changes the result to `ERROR` and records the mismatch rather than treating the create as verified.

A final-cutover preflight against **all existing Lightspeed product codes**, not only exact SKU, remains a required/recommended safeguard before the full catalog migration.

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
- `LS: importer / Migration Unmapped Brands` — ranked brand UUID gaps.
- `LS: importer / Migration Failure Review` — failed controlled writes.
- `Flagged Migration Items / Flagged Items` — dedicated durable review queue with mapping/identity/code-collision reasons and manual review fields.

Manual review fields and the existing Review Status column position are preserved when the dedicated flagged workbook is synchronized.

## Planned flow

1. Load and validate the authoritative RMS Drive export.
2. Load current `LS: importer` category, supplier, brand and UUID data.
3. Normalize source fields and reject conflicting duplicate mappings.
4. Apply migration/reorder/source-identity/code-collision rules.
5. Resolve category/supplier/brand eligibility.
6. Send unresolved or ambiguous rows to `REVIEW`.
7. Build a varied, fully mapped dry-run preview.
8. Run a bounded controlled live batch of at most 20 products and perform full read-back verification.
9. Review test results, flagged items, images, and reorder behavior.
10. Delete controlled test products before final cutover when explicitly authorized.
11. Run the full migration with resumable batching after final approval.
12. Perform physical inventory in Lightspeed to establish on-hand quantities.

## Current policy decisions still open

- Inactive RMS items with quantity > 0 are migration candidates, but controlled payloads currently set `active.in_store=True`. Decide whether final cutover should preserve RMS inactive state in Lightspeed.
- Controlled payloads currently set `active.ecwid=False`. Online-channel activation is a separate cutover decision.
- Tags are not currently part of the RMS migration payload.
- `DEFAULT_RMS_WEIGHT_UNIT = 'LB'` remains the migration assumption because RMS does not export an explicit weight-unit field. Source values are consistent with that assumption, but it should remain documented as an assumption.
- Full existing-Lightspeed code-collision preflight beyond exact SKU must be completed before final catalog cutover.

## Notes

Category/supplier mappings can change while migration cleanup continues. The pipeline always reads the latest live mapping data at execution time.

No production Railway deployment is required for the Colab migration notebook.


## Current notebook configuration

As of 2026-10-16, the audited notebook version is `2026-10-16-v46`.

- Authoritative RMS source: `My Drive/Hobby Corner Migration/HCDB Test Items.csv`.
- Legacy RMS candidate Google Sheet is emergency-only and `ALLOW_LEGACY_SOURCE_FALLBACK` defaults to `False`; setup fails closed when the Drive CSV is missing.
- Operational mapping workbook: `LS: importer`.
- Dedicated review workbook: `Flagged Migration Items` / `Flagged Items`.
- Product test batch: 20; absolute product-write cap: 20.
- Reorder test cap: 5.
- All live-write switches default to `False`.
- RMS item quantity is selection-only and opening inventory remains hard-coded to zero.

Supplier-cost precedence for the controlled payload is the primary-supplier `RMS_SupplierListCost` when available, followed by legacy item-level cost fields.


## Brand resolution

- RMS/source brand values are resolved through `LS: importer / Brands` before UUID lookup.
- `Clean Brand Name` is the canonical Lightspeed brand name.
- `Combine` is an RMS/source alias that maps to the canonical clean name.
- Exact canonical names are then resolved against the complete paginated Lightspeed brand list.
- `ENABLE_MISSING_BRAND_CREATE` follows `ENABLE_PRODUCT_CREATE_TEST`. Missing brands are created just-in-time only for products actually selected into the live write batch; brands with no selected migrating product are never created.
- Brand creation uses `POST /api/2026-04/brands`, then re-reads Lightspeed and writes the verified UUID into `Brand ID Map`.
- The workflow never renames, merges, or deletes existing Lightspeed brands automatically.
