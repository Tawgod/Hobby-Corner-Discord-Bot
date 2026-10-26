"""One-shot manual forum permissions/tag smoke test.

Run within the authenticated Hobby Bot process, never with a token printed or
passed on the command line. This helper does not launch a bot or run on import.
"""
import discord

FORUM_ID = 1471589727441260759

async def run_forum_smoke_test(bot: discord.Client):
    forum = bot.get_channel(FORUM_ID) or await bot.fetch_channel(FORUM_ID)
    if not isinstance(forum, discord.ForumChannel):
        raise TypeError("Target channel is not a Discord forum")
    me = forum.guild.me
    perms = forum.permissions_for(me)
    if not (perms.view_channel and perms.send_messages and perms.read_message_history):
        raise PermissionError("Missing permission to view, read or create forum posts")
    available = [{"id": str(t.id), "name": t.name, "moderated": t.moderated}
                 for t in forum.available_tags]
    # Deliberately create a single untagged test post to avoid accidentally
    # mapping staff status/priority tags. The forum may require a tag.
    selected = []
    if forum.flags.require_tag:
        selected = [t for t in forum.available_tags if not t.moderated][:1]
        if not selected:
            raise PermissionError("Forum requires a tag; no unmoderated tag available")
    created = await forum.create_thread(
        name="[BOT TEST] Staff task forum access",
        content="Hobby Bot permission and forum-tag integration test. No employee tasks were changed. Staff may archive this thread after verification.",
        applied_tags=selected,
        reason="Owner-requested forum permissions and tags smoke test")
    return {
        "thread_id": str(created.thread.id),
        "url": created.thread.jump_url,
        "available_tags": available,
        "applied_tags": [{"id": str(t.id), "name": getattr(t, "name", None)}
                         for t in created.thread.applied_tags]
    }
