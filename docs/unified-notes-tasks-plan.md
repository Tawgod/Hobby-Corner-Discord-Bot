# Unified Notes & Tasks — Implementation Plan (2026-10-23)

## Agreed decisions
- Staff tasks support **direct assignment** (named employee must accept/decline) and **open pool** (authorized staff can claim).
- Completion by the creator or an accepted assignee **automatically marks complete and archives** the task and **notifies the creator**. Completion summaries are optional; task and history remain searchable.
- Personal notes/tasks remain private; no automatic exposure to the Hobby Corner Discord guild.
- Discord forum ID: `1471589727441260759` (staff-tasks-and-projects).

## Current branch scope
`task_workflow.py` is a pure, storage-independent transition layer. It produces event intents but does not send Discord messages, write to PostgreSQL or run background jobs. Unit tests are in `tests/test_task_workflow.py`. No production behavior changes until integrated.

## Next implementation milestones
1. Read-only Discord discovery: validate bot permissions to view forum and thread history. Inventory tags, thread IDs, author, assignees, status, attachments. Do not create or alter existing threads in this phase.
2. Postgres schema: notes, tasks, task_assignments, task_events, users, visibility ACLs, discord_thread_links and outbox. Index source-thread ID uniquely for repeatable migration. Store private personal data separately or use strict row-level permission enforcement. Apply optimistic locking via `version`.
3. Atomic service: persist task transitions and outbox events in a transaction; unique event/dedupe keys ensure retries never double-notify. Claim from pool must be conditional on still available; first accepted claimant wins.
4. Bot commands/views: `/hc task create`, `/hc task mine`, `/hc task pool`, `/hc task assign`, plus permission-checked Accept / Decline / Update / Complete actions. Forum post and database record must carry reciprocal IDs. Restore persistent Discord UI views after process restart.
5. Completion outbox worker: notify creator by Discord DM or designated private fallback; update linked thread tags; store summary; archive forum thread after final update. Failure to DM must not undo completion, and retry must be idempotent.
6. Notes front end/API: personal capture first, then optional Google Keep import bridge. Independent service credentials (no Torn credentials or shared private notes access).
7. Only after verification: opt-in read/write migration of forum threads, then staged testing and separately approved production deployment.

## Task status map
- pool -> available -> claim -> in_progress -> completed (archived)
- direct -> pending -> accept -> in_progress -> completed (archived)
- direct -> pending -> decline -> available + creator notification
- owner may reassign active tasks; terminal tasks immutable without a separately audited reopen feature.

## Security / operations
Discord slash command access must be checked server-side using guild, staff role and employee identity; `task_workflow.py` alone does not authenticate a caller. Do not treat client-supplied actor IDs as trusted. Keep immutable audit records; never treat Discord tags as the source of truth. Notifications to owners must exclude private details if delivery channel is not private. Avoid leaking personal tasks to public forum or staff. No extra paid SaaS; Railway usage charges still apply. No live import or deployment authorized by this plan.
