# Product Migration (RMS -> Lightspeed via Colab)

This folder contains the product-migration pipeline for the Hobby Corner RMS-to-Lightspeed cutover.

## Ground rules

- RMS on-hand quantity is **never** imported as opening Lightspeed inventory.
- Opening inventory will come from the full physical inventory performed at cutover.
- RMS quantity is used only as a migration-selection signal.
- Lightspeed supplier/category destinations come from the existing **LS: importer** mappings, not raw RMS names.
- Unmapped or ambiguous supplier/category values must go to review; never guess.
- Inactive items with RMS quantity > 0 remain migration candidates, but reorder values are forced to 0.
- DoNotOrder items have reorder values forced to 0.
- Default reorder suppression starts after 12 months without a reliable sale.
- Games Workshop items receive special review because part numbers may be reused.
- The pipeline must support dry-run, small test batches, restart/resume, and explicit status logging.
- Bulk processing will run in Google Colab Pro; GitHub remains the source of truth.

## Planned flow

1. Load RMS candidate export.
2. Load current LS: importer supplier/category mappings.
3. Normalize source fields.
4. Apply migration/reorder review rules.
5. Resolve Lightspeed supplier/category mappings.
6. Send unresolved rows to REVIEW.
7. Run a 5-10 item dry test batch.
8. Verify created products in Lightspeed.
9. Delete test products before the real cutover.
10. Run the full migration with resumable batching.
11. Perform physical inventory in Lightspeed to establish on-hand quantities.

## Status values

- READY
- REVIEW
- EXCLUDE
- CREATED
- UPDATED
- ERROR

## Notes

Category mappings are still being updated in RMS and LS: importer. The pipeline must always read the latest mapping data at execution time.
