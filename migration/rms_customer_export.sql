/*
Hobby Corner RMS -> Lightspeed customer master export

Purpose:
- One row per RMS customer for Lightspeed customer create/update.
- Preserve RMS Customer.ID as the permanent migration key.
- Preserve CurrentDiscount as the initial legacy rewards fallback.
- Does NOT include rolling rewards transactions; use rms_rewards_history_export.sql for that.

Run this report immediately before the final customer migration.
*/
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
