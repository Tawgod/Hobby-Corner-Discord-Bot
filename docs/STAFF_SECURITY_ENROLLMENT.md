# Hobby Bot — Admin/Owner Security Enrollment Invitations

**Status:** In Development / Testing. Draft PR #3, not merged or deployed.

## Command

`/hc security_setup employee:@StaffMember` is available only to verified members of the configured Discord guild with the exact **Admin or Owner** role ID. The target must have a configured Staff/Admin/Owner role. The command contacts the protected backend with a separate service credential. The backend must **independently confirm** active Timeclock linkage of both Discord IDs, enforce `staff.authenticator.invite` for the manager and employee, and persist a one-time 15-minute invitation.

The employee receives an encrypted-transport HTTPS link by DM. If their DMs are disabled, a one-time link is returned ephemerally to the requesting manager for private handoff. No PIN, authenticator QR image, TOTP secret, or six-digit code is sent in Discord.

The employee signs in with their own Timeclock PIN. **A separate real manager passkey/security-key/verified IdP and exact `staff.authenticator.enroll` permission** must still approve the pending request before any QR or secret appears.

## Staging-only configuration

The following variables are **not set by this PR**; configure only in a separate isolated test bot/service. Never reuse existing Timeclock admin secrets for invitation exchange.

- `SECURITY_ENROLLMENT_API_URL`: private protected staging API base, HTTPS.
- `SECURITY_ENROLLMENT_PUBLIC_ORIGIN`: exact HTTPS user-facing staging origin; this must match the backend's `setupOrigin`.
- `SECURITY_ENROLLMENT_BOT_KEY`: dedicated 32+ character secret, matched to the backend `botSecret`.
- `SECURITY_ENROLLMENT_GUILD_ID`: exact approved Discord guild ID.
- `SECURITY_OWNER_ROLE_ID`: exact Discord Owner role ID.
- Existing `TIMECLOCK_ADMIN_ROLE_ID` and `TIMECLOCK_STAFF_ROLE_ID`: must match the test guild's roles; verify actual values without exposing credentials.

The backend needs a disposable DB and migration `db/hc_staff_security_invitations.sql`, `tokenSecret`, actual `resolveDiscordEmployee` Timeclock lookup and exact `canInvite` policy, mounted invitation and security routers, separate protected HTTPS host, a verified manager passkey and strong-identity bootstrap.

## Isolated CI

```sh
python -m py_compile main.py security_invite.py
python -m unittest discover -s tests -p 'test_security_invite.py' -v
```

Tests verify exact role IDs, bot authentication headers, safe private link paths, backend failures and rejected untrusted URLs. These do not contact real Discord, enroll an employee or deploy an endpoint.

The complete signed staff session -> manager approval -> TOTP QR -> code integration is tested by `Tawgod/Lightspeed` `tests/shared-staff-discord-invitation-postgres.test.mjs`. See `docs/STAFF_DISCORD_ENROLLMENT_TESTING.md`.

**Never merge the bot PR to main until separately authorized. Main is deployed to production.**
