# Pinned Forum Dashboard — Integration Instructions

**State:** reusable Discord view and refresh component committed; no live task backend yet. Not wired into main.py or deployed.

## User choices
- Direct assignment and shared open task pool.
- Accepted employee or creator completes => automatic completion/archive and owner notification.
- One persistent post in forum `1471589727441260759`; edit its starter message when task counts change. **Do not generate a new message on every refresh.**

## Dashboard controls
`My Tasks`, `Open Task Pool`, `New Task`, `Claim Task`, `Complete Task`, `Refresh`. Read-only lists and messages are ephemeral to the clicker. Create/claim/complete delegate to the authenticated task service, which must validate identity and permissions independently.

## Setup once the task API exists
1. Implement DashboardProvider using the task database/API. `is_staff` must check authorized guild membership/roles and an authenticated employee record. Task service must independently authorize every action.
2. Register persistent view **once** during bot initialization: `register_dashboard(bot, provider)`. Do not recreate the bot or reuse hardcoded Python in-memory state as the source of truth.
3. Create one forum thread titled `📋 Staff Task Dashboard` and starter message with `dashboard_embed` + `TaskDashboardView(provider)`. Store both IDs in the database or Railway variables `STAFF_DASHBOARD_THREAD_ID` and `STAFF_DASHBOARD_MESSAGE_ID`. This is a separate **explicitly authorized** one-time action.
4. Pin the forum post (the thread itself), via Discord UI or an appropriately permissioned bot operation. Do not pin other employees' posts or replace existing posts.
5. After bot startup, launch a single managed `dashboard_refresh_loop` with the stored IDs, e.g. every 300 seconds; cancel on shutdown. It updates only when counts change; button press can force refresh. The post should not be auto-archived. Discord forum pinned threads do not auto-archive.
6. Log refresh failures and expose stale-state / last successful update timestamps. Retry on network failure and do not create duplicate threads.
7. Verify access with owner, staff member, and unauthorized account. Never include private/personal task details in a publicly readable summary.

## Discord permissions
Use `bot` and `applications.commands` OAuth scopes; verify View Channel, Send Messages, Read Message History, Send Messages in Threads, and Manage Threads for post pinning. Forum thread creation uses Send Messages permission. Preserve portal privileged intents currently required by existing Hobby Bot commands. No Administrator grant necessary.

## AI integration
Use **existing** Railway AI Integration Gateway project and `Tawgod/ai-integration-gateway` repository. Existing test services remain staged, not deployed; gateway has no network HTTP endpoint yet. Its `src/hobby-task-ai.js` adapter builds safe, read-only task prompts but is **not authorization by itself**. Integrate with approved authentication, persistent quotas, audit logs, and the separately versioned config at `Tawgod/Hobby-Corner/integrations/ai`. Neither credentials nor private personal notes are placed in Discord.

## Remaining before activation
- Durable task DB and event/outbox implementation with idempotent owner notifications
- Concrete, server-authorized DashboardProvider and per-action task API
- Persistent view startup wiring; pinned thread creation/ID storage
- Secure gateway HTTP API and live secrets in Railway
- Automated integration tests and deployment review
