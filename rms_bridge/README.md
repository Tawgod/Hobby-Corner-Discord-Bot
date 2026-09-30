# Hobby Corner RMS Bridge

Windows-side, read-only bridge for Microsoft RMS / SQL Server.

The bridge reads RMS locally with the dedicated `hobbycorner_read` login and sends migration snapshots to the Hobby Corner Rewards API over HTTPS. It never writes to RMS.

## Setup

1. Copy this folder to the RMS server.
2. Run PowerShell as Administrator.
3. Run `.\install.ps1`.
4. Edit `C:\HobbyCorner\RMSBridge\config.json`.
5. Fill in the RMS database name, `hobbycorner_read` password, and Rewards API admin key.
6. Test with:
   `powershell -ExecutionPolicy Bypass -File C:\HobbyCorner\RMSBridge\bridge.ps1 -Action test`
7. Inspect live RMS columns with `-Action schema`.
8. Preview customers with `-Action customers-preview`.
9. Run `-Action snapshot` only after the schema and employee-exclusion rule are verified.

The transaction snapshot intentionally does not filter employee-related rows yet. The old RMS rewards report used an `Employee` condition whose table semantics need to be confirmed against the live schema before final migration.
