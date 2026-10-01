# Customer and preorder data sources

These Google Sheets are part of the Hobby Corner Discord/customer/preorder workflow.

## Master Customer List
- Spreadsheet ID: `1ZBNXQnrGIdHQO9y_83a90hr6VIUQx_JmC8csjllKnCA`
- Tabs currently used:
  - `Customer` — curated customer/Discord identity source.
  - `Form Responses 1` — Discord customer form submissions.
  - `Lightspeed Customers` — Lightspeed-related customer data/mapping.
- Hobby Bot reconciles the `Customer` tab hourly.
- Discord user ID is the durable identity key.
- Current Discord server display name is staged separately for future Lightspeed `twitter` field use.

## Preorder sheets
Folder: `1qIP8y5_kQW5ErovqPhaS6cm9DwbNDx69`

- HC Preorders
  - Spreadsheet ID: `1BkvF4yk9CPki6fk08bR_8oTeaHxZJ5u6Szva-BMwCac`
- GW Preorders
  - Spreadsheet ID: `1Eq-ZtHMlECID95uJlH6vZCzO0GnFtG5vKslkRiJovhY`
- Master Preorder Sheet
  - Spreadsheet ID: `1kjcAOxXUn2Us52cDccx87RCYJJyu4ba91ksbg63BIEE`

HC Preorders and GW Preorders are used together with the Master Customer List and Hobby Bot. Preserve this relationship when changing customer identity, Discord ID, preorder, or Lightspeed customer-mapping logic.
