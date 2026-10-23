"""One durable pinned forum dashboard, updated in place by Hobby Bot.

Integration module: does not create posts until explicitly invoked by the bot.
Requires a trusted task backend implementing DashboardProvider, and explicit
staff-role authorization. No in-memory task state is authoritative.
"""
import discord
import asyncio
import logging
from datetime import datetime, timezone
from typing import Protocol

FORUM_ID = 1471589727441260759

class DashboardProvider(Protocol):
    async def summary(self) -> dict: ...
    async def tasks_for(self, user_id: int) -> list: ...
    async def available(self, user_id: int) -> list: ...
    async def claim(self, task_id: str, user_id: int) -> str: ...
    async def complete(self, task_id: str, user_id: int) -> str: ...
    async def create(self, title: str, details: str, user_id: int) -> str: ...
    async def is_staff(self, guild_id: int, user_id: int) -> bool: ...

def dashboard_embed(data: dict) -> discord.Embed:
    embed = discord.Embed(title="📋 Hobby Corner • Staff Task Dashboard",
        description="Use the buttons below to see your work, claim an open task, or refresh. Tasks are tracked centrally.")
    counts = data.get("counts", {})
    for name, key in (("Open Pool", "available"), ("Awaiting Acceptance", "pending"),
                      ("In Progress", "in_progress"), ("Completed Today", "completed_today")):
        embed.add_field(name=name, value=str(max(0, int(counts.get(key, 0)))), inline=True)
    updated = data.get("updated_at") or datetime.now(timezone.utc).isoformat()
    embed.set_footer(text=f"Last updated: {updated} • This post updates in place")
    return embed

def task_lines(tasks: list) -> str:
    if not tasks:
        return "No tasks to show."
    lines = []
    for item in tasks[:15]:
        title = discord.utils.escape_markdown(str(item.get("title", "Untitled")))[:100]
        task_id = str(item.get("id", ""))[:64]
        lines.append(f"• **{title}** — \`{task_id}\`")
    return "\n".join(lines)

class TaskInputModal(discord.ui.Modal):
    title_text = discord.ui.TextInput(label="Task title", max_length=100)
    details = discord.ui.TextInput(label="Details", style=discord.TextStyle.paragraph, required=False, max_length=1000)

    def __init__(self, provider: DashboardProvider):
        super().__init__(title="Create Staff Task")
        self.provider = provider

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not await self.provider.is_staff(interaction.guild.id, interaction.user.id):
            await interaction.response.send_message("Staff access required.", ephemeral=True)
            return
        result = await self.provider.create(str(self.title_text), str(self.details), interaction.user.id)
        await interaction.response.send_message(result, ephemeral=True)


class TaskIdModal(discord.ui.Modal):
    task_id = discord.ui.TextInput(label="Task ID", min_length=1, max_length=64)

    def __init__(self, provider: DashboardProvider, action: str):
        super().__init__(title="Claim Task" if action == "claim" else "Complete Task")
        self.provider = provider
        self.action = action

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not await self.provider.is_staff(interaction.guild.id, interaction.user.id):
            await interaction.response.send_message("Staff access required.", ephemeral=True)
            return
        try:
            if self.action == "claim":
                result = await self.provider.claim(str(self.task_id), interaction.user.id)
            else:
                result = await self.provider.complete(str(self.task_id), interaction.user.id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(result, ephemeral=True)


class TaskDashboardView(discord.ui.View):
    def __init__(self, provider: DashboardProvider):
        super().__init__(timeout=None)
        self.provider = provider

    async def authorized(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not await self.provider.is_staff(interaction.guild.id, interaction.user.id):
            await interaction.response.send_message("Staff access is required.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="My Tasks", emoji="📋", style=discord.ButtonStyle.primary,
                       custom_id="hc:task_dashboard:mine:v1")
    async def mine(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.authorized(interaction): return
        tasks = await self.provider.tasks_for(interaction.user.id)
        await interaction.response.send_message(task_lines(tasks), ephemeral=True)

    @discord.ui.button(label="Open Task Pool", emoji="📥", style=discord.ButtonStyle.secondary,
                       custom_id="hc:task_dashboard:pool:v1")
    async def pool(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.authorized(interaction): return
        tasks = await self.provider.available(interaction.user.id)
        await interaction.response.send_message(task_lines(tasks) +
            "\nUse the task's claim action to accept an available task.", ephemeral=True)

    @discord.ui.button(label="New Task", emoji="➕", style=discord.ButtonStyle.success,
                       custom_id="hc:task_dashboard:create:v1")
    async def create_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.authorized(interaction): return
        await interaction.response.send_modal(TaskInputModal(self.provider))

    @discord.ui.button(label="Claim Task", emoji="🙋", style=discord.ButtonStyle.secondary,
                       custom_id="hc:task_dashboard:claim:v1")
    async def claim_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.authorized(interaction): return
        await interaction.response.send_modal(TaskIdModal(self.provider, "claim"))

    @discord.ui.button(label="Complete Task", emoji="✅", row=1, style=discord.ButtonStyle.success,
                       custom_id="hc:task_dashboard:complete:v1")
    async def complete_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.authorized(interaction): return
        await interaction.response.send_modal(TaskIdModal(self.provider, "complete"))

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary,
                       custom_id="hc:task_dashboard:refresh:v1")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.authorized(interaction): return
        summary = await self.provider.summary()
        if interaction.message:
            await interaction.message.edit(embed=dashboard_embed(summary), view=self)
        await interaction.response.send_message("Dashboard refreshed.", ephemeral=True)

async def ensure_dashboard(bot: discord.Client, provider: DashboardProvider, *,
                           thread_id: int, message_id: int) -> None:
    """Only update a configured existing dashboard; never duplicate it.

    Dashboard creation and forum pinning are a separate, authorized setup operation.
    The operator supplies saved IDs from durable task configuration.
    """
    if not thread_id or not message_id:
        raise ValueError("A configured dashboard thread/message is required")
    thread = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
    if not isinstance(thread, discord.Thread) or thread.parent_id != FORUM_ID:
        raise ValueError("Dashboard thread must belong to staff forum")
    starter = await thread.fetch_message(message_id)
    if starter.author.id != bot.user.id:
        raise ValueError("Dashboard message is not owned by this bot")
    await starter.edit(embed=dashboard_embed(await provider.summary()),
                       view=TaskDashboardView(provider))

def register_dashboard(bot: discord.Client, provider: DashboardProvider) -> None:
    """Call exactly once at startup so buttons survive bot restart."""
    bot.add_view(TaskDashboardView(provider))

async def dashboard_refresh_loop(bot: discord.Client, provider: DashboardProvider, *,
                                 thread_id: int, message_id: int, interval_seconds: int = 300):
    """Run as one managed startup task after ready; cancel on bot shutdown.

    Edit only when counters change, rather than artificially bumping the timestamp
    on every poll. Store thread and message IDs in durable configuration.
    """
    if interval_seconds < 60:
        raise ValueError("Minimum refresh interval is 60 seconds")
    previous = None
    while not bot.is_closed():
        try:
            data = await provider.summary()
            counts = data.get("counts", {})
            snapshot = tuple(sorted((str(k), int(v)) for k, v in counts.items()))
            if snapshot != previous:
                await ensure_dashboard(bot, provider, thread_id=thread_id, message_id=message_id)
                previous = snapshot
        except (discord.HTTPException, ValueError, OSError):
            logging.exception("Staff dashboard refresh failed")
        await asyncio.sleep(interval_seconds)
