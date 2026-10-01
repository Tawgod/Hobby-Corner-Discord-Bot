# RMS customer migration contract

## Customer master report

The customer importer should consume columns by **header name**, not fixed column position.

Required migration fields:

- `RMSCustomerID` — immutable RMS `Customer.ID`; the cross-system migration key.
- `FirstName`, `LastName`, `Company`
- `EmailAddress`, `PhoneNumber`, `FaxNumber`
- `Address`, `Address2`, `City`, `State`, `Zip`, `Country`
- `TaxNumber`, `TaxExempt`
- `AccountBalance`, `TotalSales`, `AccountOpened`, `LastVisit`, `TotalVisits`, `TotalSavings`
- `LegacyDiscount` — RMS CurrentDiscount at cutover.
- `Vouchers`, `LastUpdated`

## Lightspeed customer import behavior

1. Match an already-imported customer by the migration map first: `RMSCustomerID -> Lightspeed UUID`.
2. If no map exists, create the customer through the current Lightspeed customers API.
3. Immediately persist the returned Lightspeed customer UUID with the RMS customer ID.
4. Store `RMSCustomerID` as a hidden customer custom field named `rms_customer_id`.
5. Store the old RMS discount as migration state in the rewards backend (`legacy_discount`), not as the permanent calculated rewards tier.
6. Do not assign rewards customer groups.
7. Do not calculate the new tier inside the customer importer. The Railway rewards worker owns rewards calculations.
8. Failed rows must go to the customer's error queue/sheet with the RMSCustomerID and error message so reruns are idempotent.
9. Successful rows should be removed/moved from the pending import sheet only after both Lightspeed creation/update and the identity-map write succeed.

## Lightspeed field mapping

| RMS export | Lightspeed customer |
|---|---|
| FirstName | first_name |
| LastName | last_name |
| Company | company_name |
| EmailAddress | email |
| PhoneNumber | phone |
| FaxNumber | fax |
| Address | physical_address_1 |
| Address2 | physical_address_2 |
| City | physical_city |
| State | physical_state |
| Zip | physical_postcode |
| TaxNumber | tax_id |
| RMSCustomerID | hidden custom field `rms_customer_id` |

Country should be normalized to the ISO country value expected by the active Lightspeed API before sending.

## Rewards legacy history report

Use `rms_rewards_history_export.sql`. Each row has:

- RMSCustomerID
- TransactionNumber
- SaleDate
- GrossTotal
- SalesTax
- PretaxAmount

The migration loader resolves RMSCustomerID through the identity map and writes the dated pre-tax transaction to the rewards database. TransactionNumber should be included in the source reference to make imports idempotent.

## Migration safety

The customer importer may be rerun. It must never create a second Lightspeed customer merely because contact details changed or are duplicated; once an RMSCustomerID has a Lightspeed UUID mapping, that mapping is authoritative.


## Employee and legacy special-discount accounts

RMS `Customer.Employee = 1` is broader than literal store employees. Live RMS data shows it is also used for legacy special-discount accounts such as club/organization discount records.

Migration behavior:

- Import these customer records into Lightspeed normally.
- Preserve `RMSCustomerID`, `AccountNumber`, `Employee`, and the original `LegacyDiscount`.
- Mark `Employee = 1` customers as excluded from the automatic rolling rewards calculation.
- Do not overwrite their legacy special discount with the automatic 0/5/7/9/12 rewards tier during migration.
- Keep the legacy/special discount separate from the calculated rewards fields so it can be reviewed or handled by a future explicit override policy.
- Transaction-history migration excludes purchases whose customer record has `Employee = 1`, matching the old RMS rewards logic.

## Account balance

RMS `AccountBalance` is not synonymous with store credit. Preserve the existing importer behavior that only treats a negative balance as customer store credit. Positive balances should not be issued as store credit.


## Legacy discount classification confirmed from RMS

Live RMS counts show the automatic rewards tiers are used by normal customers at 0%, 5%, 7%, 9%, and 12%.

Eight non-employee customers have a 10% CurrentDiscount. Treat these as legacy/special manual discounts, not as an automatic rewards tier.

Employee-flagged RMS records use 0%, 10%, 15%, 20%, and 30% discounts. These records are excluded from automatic rolling rewards. If their legacy discount is greater than zero, preserve it as a separate special discount.

Classification at customer mapping time:

- Employee = 0 and LegacyDiscount in {0,5,7,9,12}: automatic rewards participant.
- Employee = 0 and LegacyDiscount outside {0,5,7,9,12}: special/manual discount; preserve separately.
- Employee = 1 and LegacyDiscount = 0: excluded from automatic rewards, no special discount.
- Employee = 1 and LegacyDiscount > 0: excluded from automatic rewards and preserve LegacyDiscount as special discount.

The automatic rewards override field remains reserved for an explicit manual override of the rolling rewards system. It should not be reused to hold RMS special discounts.
