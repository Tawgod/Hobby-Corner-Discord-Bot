/*
Hobby Corner RMS -> Rewards legacy transaction history

Purpose:
- Export each qualifying customer transaction in the rolling rewards window.
- The Railway rewards service imports these dated transactions so purchases
  age out naturally during the first 91 days after conversion.
- PretaxAmount removes the exact RMS SalesTax amount; no assumed tax rate.
- Employee = 1 matches the exclusion in the old rewards query.

IMPORTANT:
Run this on/just before cutover. REWARDS_LOOKBACK_DAYS defaults to 91 in Railway.
*/
SELECT
    T.CustomerID                                  AS RMSCustomerID,
    T.TransactionNumber,
    T.Time                                        AS SaleDate,
    CAST(T.Total AS decimal(14,2))                 AS GrossTotal,
    CAST(ISNULL(T.SalesTax, 0) AS decimal(14,2))  AS SalesTax,
    CAST(T.Total - ISNULL(T.SalesTax, 0)
         AS decimal(14,2))                         AS PretaxAmount
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
