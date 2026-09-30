import os
import json
import discord
import gspread
import re
import asyncio
import requests
from discord.ext import commands, tasks
from discord import app_commands
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta, timezone

# ==========================================
# 1. GOOGLE SHEETS AUTHENTICATION
# ==========================================
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
if creds_json:
    creds_dict = json.loads(creds_json)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
else:
    print("WARNING: GOOGLE_SERVICE_ACCOUNT_JSON environment variable not found.")
    client = None

# ==========================================
# 2. BOT CONFIGURATION
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

TIMECLOCK_API_URL = os.environ.get("TIMECLOCK_API_URL", "").rstrip("/")
TIMECLOCK_ADMIN_SECRET = os.environ.get("TIMECLOCK_ADMIN_SECRET", "")
TIMECLOCK_ADMIN_ROLE_ID = os.environ.get("TIMECLOCK_ADMIN_ROLE_ID", "").strip()
TIMECLOCK_STAFF_ROLE_ID = os.environ.get("TIMECLOCK_STAFF_ROLE_ID", "").strip()

channel_map_env = os.environ.get("CHANNEL_SHEET_MAP")
if channel_map_env:
    CHANNEL_MAP = json.loads(channel_map_env)
else:
    CHANNEL_MAP = {
        "1340074486783021169": {
            "sheet_id": "YOUR_SHEET_ID_HERE", 
            "tab_name": "Raw Data"
        }
    }

def has_role_id(member, role_id):
    if not role_id:
        return False
    return any(str(role.id) == role_id for role in getattr(member, "roles", []))

def has_timeclock_admin_role(member):
    return has_role_id(member, TIMECLOCK_ADMIN_ROLE_ID)

def has_timeclock_staff_access(member):
    return has_timeclock_admin_role(member) or has_role_id(member, TIMECLOCK_STAFF_ROLE_ID)



# ==========================================
# HOBBY CORNER SLASH COMMANDS
# ==========================================
hc_group = app_commands.Group(
    name="hc",
    description="Hobby Corner staff tools"
)

async def require_timeclock_admin(interaction: discord.Interaction):
    if interaction.guild is None or not has_timeclock_admin_role(interaction.user):
        if interaction.response.is_done():
            await interaction.followup.send(
                "You do not have access to this Hobby Corner timeclock command.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "You do not have access to this Hobby Corner timeclock command.",
                ephemeral=True
            )
        return False
    return True

def timeclock_headers():
    return {"x-timeclock-admin-secret": TIMECLOCK_ADMIN_SECRET}

async def send_employee_list(interaction: discord.Interaction):
    response = requests.get(
        f"{TIMECLOCK_API_URL}/api/timeclock/admin/employees",
        headers=timeclock_headers(),
        timeout=10
    )
    data = response.json() if response.content else {}

    if not response.ok:
        await interaction.followup.send(
            f"Could not load employees: {data.get('error', response.text)}",
            ephemeral=True
        )
        return

    employees = data.get("employees", [])
    if not employees:
        await interaction.followup.send("No timeclock employee records exist yet.", ephemeral=True)
        return

    entries = []
    for employee in employees:
        discord_label = "unlinked"
        if employee.get("discord_user_id"):
            display = employee.get("discord_display_name") or employee.get("discord_username") or "Discord user"
            username = employee.get("discord_username") or "?"
            discord_label = f"{display} (@{username}) / {employee.get('discord_user_id')}"

        lightspeed_label = employee.get("lightspeed_user_id") or "not linked"
        pin_label = "yes" if employee.get("has_pin") else "no"

        entries.append(
            f"**{employee.get('name')}**\n"
            f"> Employee ID: **{employee.get('id')}**\n"
            f"> Lightspeed ID: {lightspeed_label}\n"
            f"> Discord: {discord_label}\n"
            f"> PIN: {pin_label}"
        )

    pages = []
    current = "**Timeclock Employees**\n\n"
    for entry in entries:
        addition = entry + "\n\n"
        if len(current) + len(addition) > 1800:
            pages.append(current.rstrip())
            current = "**Timeclock Employees (continued)**\n\n" + addition
        else:
            current += addition

    if current.strip():
        pages.append(current.rstrip())

    for page in pages:
        await interaction.followup.send(page, ephemeral=True)

async def send_review_queue(interaction: discord.Interaction):
    response = requests.get(
        f"{TIMECLOCK_API_URL}/api/timeclock/admin/review",
        headers=timeclock_headers(),
        timeout=10
    )
    data = response.json() if response.content else {}

    if not response.ok:
        await interaction.followup.send(
            f"Could not load review queue: {data.get('error', response.text)}",
            ephemeral=True
        )
        return

    entries = data.get("entries", [])
    if not entries:
        await interaction.followup.send("No timeclock entries currently need review.", ephemeral=True)
        return

    lines = []
    for entry in entries[:20]:
        lines.append(
            f"**Entry {entry.get('id')} — {entry.get('employee_name')}**\n"
            f"In: {entry.get('clock_in')}\n"
            f"Out: {entry.get('clock_out')}\n"
            f"Status: {entry.get('status')}"
        )

    await interaction.followup.send(
        "**Timeclock Review Queue**\n\n" + "\n\n".join(lines),
        ephemeral=True
    )

class LinkEmployeeModal(discord.ui.Modal, title="Link Timeclock Employee"):
    employee_id = discord.ui.TextInput(
        label="Employee ID",
        placeholder="Use the ID shown in Employees",
        required=True,
        max_length=20
    )
    pin = discord.ui.TextInput(
        label="Initial 4-digit PIN",
        placeholder="1234",
        required=True,
        min_length=4,
        max_length=4
    )

    def __init__(self, member: discord.Member):
        super().__init__()
        self.member = member

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_timeclock_admin(interaction):
            return

        pin_value = str(self.pin).strip()
        if not re.fullmatch(r"\d{4}", pin_value):
            await interaction.response.send_message("PIN must be exactly 4 digits.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            response = requests.post(
                f"{TIMECLOCK_API_URL}/api/timeclock/admin/link-discord",
                headers=timeclock_headers(),
                json={
                    "employeeId": str(self.employee_id).strip(),
                    "discordUserId": str(self.member.id),
                    "discordUsername": self.member.name,
                    "discordDisplayName": self.member.display_name,
                    "pin": pin_value,
                    "actor": str(interaction.user)
                },
                timeout=10
            )
            data = response.json() if response.content else {}

            if response.ok:
                employee = data.get("employee", {})
                await interaction.followup.send(
                    f"Linked **{self.member.display_name}** to "
                    f"**Employee ID {employee.get('id')} — {employee.get('name')}** "
                    "and assigned the initial PIN.",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"Could not link employee: {data.get('error', response.text)}",
                    ephemeral=True
                )
        except Exception as e:
            await interaction.followup.send(f"Timeclock API error: {e}", ephemeral=True)

class LinkMemberSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Choose the Discord member to link",
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        member = self.values[0]
        await interaction.response.send_modal(LinkEmployeeModal(member))

class LinkMemberView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(LinkMemberSelect())

class ResetPinModal(discord.ui.Modal, title="Reset Timeclock PIN"):
    pin = discord.ui.TextInput(
        label="New 4-digit PIN",
        placeholder="1234",
        required=True,
        min_length=4,
        max_length=4
    )

    def __init__(self, member: discord.Member):
        super().__init__()
        self.member = member

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_timeclock_admin(interaction):
            return

        pin_value = str(self.pin).strip()
        if not re.fullmatch(r"\d{4}", pin_value):
            await interaction.response.send_message("PIN must be exactly 4 digits.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            response = requests.post(
                f"{TIMECLOCK_API_URL}/api/timeclock/admin/assign-pin",
                headers=timeclock_headers(),
                json={
                    "discordUserId": str(self.member.id),
                    "pin": pin_value
                },
                timeout=10
            )
            data = response.json() if response.content else {}

            if response.ok:
                await interaction.followup.send(
                    f"Updated the timeclock PIN for **{self.member.display_name}**.",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"Could not update PIN: {data.get('error', response.text)}",
                    ephemeral=True
                )
        except Exception as e:
            await interaction.followup.send(f"Timeclock API error: {e}", ephemeral=True)

class ResetPinMemberSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Choose the employee whose PIN should change",
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        member = self.values[0]
        await interaction.response.send_modal(ResetPinModal(member))

class ResetPinMemberView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(ResetPinMemberSelect())

def format_entry_choice(entry):
    clock_in = str(entry.get("clock_in") or "")
    clock_out = str(entry.get("clock_out") or "OPEN")
    name = str(entry.get("employee_name") or "Employee")
    entry_id = str(entry.get("id"))

    date_part = clock_in[:10] if len(clock_in) >= 10 else "unknown date"
    time_part = clock_in[11:16] if len(clock_in) >= 16 else ""
    label = f"#{entry_id} • {name} • {date_part} {time_part}".strip()
    return label[:100], clock_out


class EntrySelect(discord.ui.Select):
    def __init__(self, entries):
        self.entry_map = {str(entry.get("id")): entry for entry in entries}
        options = []

        for entry in entries:
            label, clock_out = format_entry_choice(entry)
            status = str(entry.get("status") or "")
            review = " • needs review" if entry.get("needs_review") else ""
            description = f"{status} • out {clock_out}{review}"[:100]
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(entry.get("id")),
                    description=description
                )
            )

        super().__init__(
            placeholder="Select an entry to edit",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        if not await require_timeclock_admin(interaction):
            return

        entry = self.entry_map.get(self.values[0])
        if not entry:
            await interaction.response.send_message("That entry is no longer available.", ephemeral=True)
            return

        await interaction.response.send_modal(FixEntryModal(entry=entry))


class EntrySelectView(discord.ui.View):
    def __init__(self, entries):
        super().__init__(timeout=180)
        self.add_item(EntrySelect(entries))


async def send_entry_results(interaction: discord.Interaction, params=None, title="Timeclock Entries"):
    response = requests.get(
        f"{TIMECLOCK_API_URL}/api/timeclock/admin/entries",
        headers=timeclock_headers(),
        params=params or {},
        timeout=10
    )
    data = response.json() if response.content else {}

    if not response.ok:
        await interaction.followup.send(
            f"Could not load entries: {data.get('error', response.text)}",
            ephemeral=True
        )
        return

    entries = data.get("entries", [])
    if not entries:
        await interaction.followup.send("No timeclock entries matched that search.", ephemeral=True)
        return

    chunks = [entries[i:i + 25] for i in range(0, len(entries), 25)]

    for index, chunk in enumerate(chunks, start=1):
        lines = []
        for entry in chunk:
            out_value = entry.get("clock_out") or "OPEN"
            review = " ⚠ needs review" if entry.get("needs_review") else ""
            lines.append(
                f"**Entry ID: {entry.get('id')} — {entry.get('employee_name')}**\n"
                f"> Clock in: {entry.get('clock_in')}\n"
                f"> Clock out: {out_value}\n"
                f"> Status: {entry.get('status')}{review}"
            )

        heading = title if len(chunks) == 1 else f"{title} — page {index}/{len(chunks)}"
        message = (
            f"**{heading}**\n\n" +
            "\n\n".join(lines) +
            "\n\nSelect an entry below to edit it."
        )

        await interaction.followup.send(
            message[:1900],
            view=EntrySelectView(chunk),
            ephemeral=True
        )


class EntrySearchModal(discord.ui.Modal, title="Search Timeclock Entries"):
    date = discord.ui.TextInput(
        label="Date",
        placeholder="YYYY-MM-DD",
        required=True,
        max_length=10
    )
    employee_id = discord.ui.TextInput(
        label="Employee ID (optional)",
        placeholder="Leave blank for all employees",
        required=False,
        max_length=20
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_timeclock_admin(interaction):
            return

        date_value = str(self.date).strip()
        employee_value = str(self.employee_id).strip()

        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value):
            await interaction.response.send_message("Date must be YYYY-MM-DD.", ephemeral=True)
            return
        if employee_value and not employee_value.isdigit():
            await interaction.response.send_message("Employee ID must be a number.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        params = {"date": date_value}
        if employee_value:
            params["employeeId"] = employee_value

        try:
            await send_entry_results(
                interaction,
                params=params,
                title=f"Entries for {date_value}"
            )
        except Exception as e:
            await interaction.followup.send(f"Timeclock API error: {e}", ephemeral=True)


class EntryBrowserView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Current Pay Period", style=discord.ButtonStyle.primary, emoji="📅")
    async def current_period(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await send_entry_results(
                interaction,
                params={"period": "current"},
                title="Current Pay Period Entries"
            )
        except Exception as e:
            await interaction.followup.send(f"Timeclock API error: {e}", ephemeral=True)

    @discord.ui.button(label="Search by Date", style=discord.ButtonStyle.secondary, emoji="🔎")
    async def search_date(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.send_modal(EntrySearchModal())


class FixEntryModal(discord.ui.Modal, title="Correct Timeclock Entry"):
    def __init__(self, entry=None):
        super().__init__()
        entry = entry or {}

        self.entry_id_input = discord.ui.TextInput(
            label="Entry ID",
            required=True,
            max_length=20,
            default=str(entry.get("id") or "")
        )
        self.clock_in_input = discord.ui.TextInput(
            label="Clock In",
            placeholder="2026-09-30T09:00:00-05:00",
            required=True,
            default=str(entry.get("clock_in") or "")
        )
        self.clock_out_input = discord.ui.TextInput(
            label="Clock Out",
            placeholder="2026-09-30T17:00:00-05:00",
            required=True,
            default=str(entry.get("clock_out") or "")
        )
        self.reason_input = discord.ui.TextInput(
            label="Reason",
            placeholder="Forgot to clock out",
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=500
        )

        self.add_item(self.entry_id_input)
        self.add_item(self.clock_in_input)
        self.add_item(self.clock_out_input)
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_timeclock_admin(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        entry_id = str(self.entry_id_input).strip()

        try:
            response = requests.post(
                f"{TIMECLOCK_API_URL}/api/timeclock/admin/entries/{entry_id}/adjust",
                headers=timeclock_headers(),
                json={
                    "clockIn": str(self.clock_in_input).strip(),
                    "clockOut": str(self.clock_out_input).strip(),
                    "reason": str(self.reason_input).strip(),
                    "actor": str(interaction.user)
                },
                timeout=10
            )
            data = response.json() if response.content else {}

            if response.ok:
                entry = data.get("entry", {})
                await interaction.followup.send(
                    f"Corrected entry **{entry_id}** for "
                    f"**{entry.get('employeeName', 'employee')}**. "
                    "The original values remain in the audit trail.",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"Could not correct entry: {data.get('error', response.text)}",
                    ephemeral=True
                )
        except Exception as e:
            await interaction.followup.send(f"Timeclock API error: {e}", ephemeral=True)


class TimeclockControlPanel(discord.ui.View):
    def __init__(self, is_admin: bool):
        super().__init__(timeout=300)
        self.is_admin = is_admin

        if not is_admin:
            for item in self.children:
                item.disabled = True

    @discord.ui.button(label="Employees", style=discord.ButtonStyle.primary, emoji="👥")
    async def employees_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await send_employee_list(interaction)
        except Exception as e:
            await interaction.followup.send(f"Timeclock API error: {e}", ephemeral=True)

    @discord.ui.button(label="Link Employee", style=discord.ButtonStyle.success, emoji="🔗")
    async def link_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.send_message(
            "Choose the Discord member to link:",
            view=LinkMemberView(),
            ephemeral=True
        )

    @discord.ui.button(label="Reset PIN", style=discord.ButtonStyle.secondary, emoji="🔢")
    async def pin_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.send_message(
            "Choose the linked employee:",
            view=ResetPinMemberView(),
            ephemeral=True
        )

    @discord.ui.button(label="Review Queue", style=discord.ButtonStyle.secondary, emoji="⚠️")
    async def review_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await send_review_queue(interaction)
        except Exception as e:
            await interaction.followup.send(f"Timeclock API error: {e}", ephemeral=True)

    @discord.ui.button(label="Browse Entries", style=discord.ButtonStyle.secondary, emoji="📋")
    async def browse_entries_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.send_message(
            "Choose how you want to find a timeclock entry:",
            view=EntryBrowserView(),
            ephemeral=True
        )

    @discord.ui.button(label="Fix Entry", style=discord.ButtonStyle.danger, emoji="🛠️")
    async def fix_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.send_modal(FixEntryModal())

@hc_group.command(name="timeclock", description="Open the Hobby Corner timeclock control panel")
async def hc_timeclock(interaction: discord.Interaction):
    if interaction.guild is None or not has_timeclock_staff_access(interaction.user):
        await interaction.response.send_message(
            "You do not have access to Hobby Corner timeclock tools.",
            ephemeral=True
        )
        return

    if not TIMECLOCK_API_URL or not TIMECLOCK_ADMIN_SECRET:
        await interaction.response.send_message(
            "Timeclock administration is not configured.",
            ephemeral=True
        )
        return

    is_admin = has_timeclock_admin_role(interaction.user)
    description = (
        "Choose an admin action below."
        if is_admin
        else "Your staff access is active. Employee self-service buttons will be added here next."
    )

    await interaction.response.send_message(
        f"**Hobby Corner Timeclock**\n{description}",
        view=TimeclockControlPanel(is_admin),
        ephemeral=True
    )

bot.tree.add_command(hc_group)


# --- GLOBAL MEMORY CACHES & QUEUES ---
MESSAGE_CACHE = {}       # Remembers SKUs to avoid Discord rate limits
SHEET_WRITE_QUEUE = []   # Holds additions
SHEET_DELETE_QUEUE = []  # Holds removals


# ==========================================
# 3. BACKGROUND ENGINE: BATCH PROCESSOR
# ==========================================
@tasks.loop(seconds=15)
async def batch_write_to_sheets():
    global SHEET_WRITE_QUEUE, SHEET_DELETE_QUEUE
    
    # ----------------------------------------
    # PART A: PROCESS ADDITIONS (WRITES)
    # ----------------------------------------
    if SHEET_WRITE_QUEUE:
        batch_to_process = SHEET_WRITE_QUEUE[:]
        SHEET_WRITE_QUEUE.clear()

        grouped_data = {}
        for item in batch_to_process:
            s_id = item["sheet_id"]
            t_name = item["tab_name"]
            
            if s_id not in grouped_data:
                grouped_data[s_id] = {}
            if t_name not in grouped_data[s_id]:
                grouped_data[s_id][t_name] = []
                
            grouped_data[s_id][t_name].append(item["row_data"])

        for s_id, tabs in grouped_data.items():
            try:
                sheet = client.open_by_key(s_id)
                for t_name, rows in tabs.items():
                    worksheet = sheet.worksheet(t_name)
                    worksheet.append_rows(rows)
                    print(f"✅ BATCH SUCCESS: Uploaded {len(rows)} orders to '{t_name}'.")
                    
            except Exception as e:
                print(f"❌ BATCH WRITE ERROR: Failed to upload to Google Sheets: {e}")
                SHEET_WRITE_QUEUE.extend(batch_to_process)

    # ----------------------------------------
    # PART B: PROCESS REMOVALS (DELETES)
    # ----------------------------------------
    if SHEET_DELETE_QUEUE:
        deletes_to_process = SHEET_DELETE_QUEUE[:]
        SHEET_DELETE_QUEUE.clear()

        del_grouped = {}
        for item in deletes_to_process:
            s_id = item["sheet_id"]
            t_name = item["tab_name"]
            
            if s_id not in del_grouped:
                del_grouped[s_id] = {}
            if t_name not in del_grouped[s_id]:
                del_grouped[s_id][t_name] = []
                
            del_grouped[s_id][t_name].append(item)

        for s_id, tabs in del_grouped.items():
            try:
                sheet = client.open_by_key(s_id)
                for t_name, del_items in tabs.items():
                    worksheet = sheet.worksheet(t_name)
                    
                    # ONE SINGLE READ API CALL FOR ALL DELETIONS!
                    all_rows = worksheet.get_all_values()
                    rows_to_delete = []

                    # Find the specific row numbers to delete
                    for d_item in del_items:
                        for i in range(len(all_rows) - 1, 0, -1):
                            row = all_rows[i]
                            if len(row) >= 5 and row[0] == d_item["user_name"] and row[1] == d_item["sku"]:
                                if (i + 1) not in rows_to_delete: # Avoid double deleting the same row
                                    rows_to_delete.append(i + 1)
                                    break # Only delete the most recent pending order for this person/SKU
                    
                    if rows_to_delete:
                        # Sort row numbers highest to lowest so we delete from the bottom up.
                        # If we deleted top-down, the row numbers below it would shift and break!
                        rows_to_delete.sort(reverse=True)
                        for r_idx in rows_to_delete:
                            worksheet.delete_rows(r_idx)
                        print(f"✅ BATCH DELETE SUCCESS: Removed {len(rows_to_delete)} orders from '{t_name}'.")

            except Exception as e:
                print(f"❌ BATCH DELETE ERROR: Failed to delete from Google Sheets: {e}")
                SHEET_DELETE_QUEUE.extend(deletes_to_process)


# ==========================================
# 4. BOT EVENTS & LISTENERS
# ==========================================
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    if not getattr(bot, "_hc_commands_synced", False):
        try:
            for guild in bot.guilds:
                bot.tree.copy_global_to(guild=guild)
                guild_synced = await bot.tree.sync(guild=guild)
                print(
                    f"✅ Synced {len(guild_synced)} Hobby Corner command group(s) "
                    f"directly to guild {guild.name} ({guild.id})."
                )

            bot.tree.clear_commands(guild=None)
            cleared = await bot.tree.sync()
            print(f"✅ Cleared global Discord application commands ({len(cleared)} remain).")

            bot._hc_commands_synced = True
        except Exception as e:
            print(f"❌ Slash command sync failed: {e}")

    if not batch_write_to_sheets.is_running():
        batch_write_to_sheets.start()
        print("✅ Batch upload engine started.")

@bot.event
async def on_raw_reaction_add(payload):
    if client is None:
        return

    channel_id_str = str(payload.channel_id)
    message_id_str = str(payload.message_id)

    if channel_id_str not in CHANNEL_MAP:
        return

    user = await bot.fetch_user(payload.user_id)
    if user.bot: 
        return
        
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    destination = CHANNEL_MAP[channel_id_str]
    target_sheet_id = destination["sheet_id"]
    target_tab_name = destination["tab_name"]

    try:
        if message_id_str in MESSAGE_CACHE:
            product_name = MESSAGE_CACHE[message_id_str]["product"]
            sku = MESSAGE_CACHE[message_id_str]["sku"]
        else:
            channel = bot.get_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            
            product_name = message.embeds[0].title if message.embeds else "Unknown Product"
            
            full_text = ""
            if message.embeds:
                for embed in message.embeds:
                    full_text += str(embed.title) + " "
                    full_text += str(embed.description) + " "
                    if embed.footer: full_text += str(embed.footer.text) + " "
                    for field in embed.fields: full_text += str(field.name) + " " + str(field.value) + " "
            else:
                full_text = message.content
                
            full_text = full_text.replace("*", "").replace("_", "")
            sku_match = re.search(r'SKU:\s*([A-Za-z0-9_-]+)', full_text, re.IGNORECASE)
            sku = sku_match.group(1) if sku_match else "NO_SKU"

            MESSAGE_CACHE[message_id_str] = {
                "product": product_name,
                "sku": sku
            }

        SHEET_WRITE_QUEUE.append({
            "sheet_id": target_sheet_id,
            "tab_name": target_tab_name,
            "row_data": [user.name, sku, product_name, "1", "Pending", timestamp, str(user.id)]
        })
        
        print(f"Queued for ADD: {user.name}'s order for {sku}")
        
    except Exception as e:
        print(f"Error processing reaction: {e}")

@bot.event
async def on_raw_reaction_remove(payload):
    channel_id_str = str(payload.channel_id)
    message_id_str = str(payload.message_id)

    if channel_id_str not in CHANNEL_MAP:
        return

    user = await bot.fetch_user(payload.user_id)
    if user.bot: 
        return
    
    destination = CHANNEL_MAP[channel_id_str]
    target_sheet_id = destination["sheet_id"]
    target_tab_name = destination["tab_name"]

    if client is None:
        return

    try:
        if message_id_str in MESSAGE_CACHE:
            sku = MESSAGE_CACHE[message_id_str]["sku"]
        else:
            channel = bot.get_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            
            full_text = ""
            if message.embeds:
                for embed in message.embeds:
                    full_text += str(embed.title) + " "
                    full_text += str(embed.description) + " "
                    if embed.footer: full_text += str(embed.footer.text) + " "
                    for field in embed.fields: full_text += str(field.name) + " " + str(field.value) + " "
            else:
                full_text = message.content
                
            full_text = full_text.replace("*", "").replace("_", "")
            sku_match = re.search(r'SKU:\s*([A-Za-z0-9_-]+)', full_text, re.IGNORECASE)
            sku = sku_match.group(1) if sku_match else "NO_SKU"

        # --- THE QUEUE INTERCEPTOR ---
        # If the user rapidly unclicked, delete their order from the waiting room.
        for i, item in enumerate(SHEET_WRITE_QUEUE):
            if item["sheet_id"] == target_sheet_id and item["tab_name"] == target_tab_name:
                row_data = item["row_data"]
                if row_data[0] == user.name and row_data[1] == sku:
                    SHEET_WRITE_QUEUE.pop(i)
                    print(f"Intercepted: Removed {user.name}'s order for {sku} from the Write Queue (No API Call).")
                    return

        # --- QUEUE FOR GOOGLE SHEETS DELETION ---
        # If it wasn't in the waiting room, it's already on the sheet. Queue the deletion.
        SHEET_DELETE_QUEUE.append({
            "sheet_id": target_sheet_id,
            "tab_name": target_tab_name,
            "user_name": user.name,
            "sku": sku
        })
        print(f"Queued for REMOVAL: {user.name}'s order for {sku}")

    except Exception as e:
        print(f"Error queuing reaction removal: {e}")

# ==========================================
# 5. ADMIN COMMAND: BULK RECOVERY
# ==========================================
@bot.event
async def on_message(message):
    if message.author.bot: 
        return

    if message.content.startswith("!timeclock"):
        if not message.guild or not has_timeclock_staff_access(message.author):
            return

    if message.content.startswith("!timeclockreview"):
        if not has_timeclock_admin_role(message.author):
            return
        if not TIMECLOCK_API_URL or not TIMECLOCK_ADMIN_SECRET:
            await message.reply("❌ Timeclock administration is not configured on the bot.")
            return

        try:
            response = requests.get(
                f"{TIMECLOCK_API_URL}/api/timeclock/admin/review",
                headers={"x-timeclock-admin-secret": TIMECLOCK_ADMIN_SECRET},
                timeout=10
            )
            data = response.json() if response.content else {}

            if not response.ok:
                await message.reply(f"❌ Could not load review queue: {data.get('error', response.text)}")
                return

            entries = data.get("entries", [])
            if not entries:
                await message.reply("✅ No timeclock entries currently need review.")
                return

            lines = []
            for entry in entries[:20]:
                lines.append(
                    f"Entry {entry.get('id')} — {entry.get('employee_name')}\n"
                    f"> In: {entry.get('clock_in')}\n"
                    f"> Out: {entry.get('clock_out')}\n"
                    f"> Status: {entry.get('status')}"
                )

            await message.reply(
                "**⏱️ Timeclock Review Queue**\n" +
                "\n".join(lines) +
                "\n\nUse !timeclockfix <entry_id> <clock_in_iso> <clock_out_iso> <reason> to correct one."
            )
        except Exception as e:
            await message.reply(f"❌ Timeclock API error: {e}")
        return

    if message.content.startswith("!timeclockfix"):
        if not has_timeclock_admin_role(message.author):
            return
        if not TIMECLOCK_API_URL or not TIMECLOCK_ADMIN_SECRET:
            await message.reply("❌ Timeclock administration is not configured on the bot.")
            return

        parts = message.content.split(maxsplit=4)
        if len(parts) < 5:
            await message.reply(
                "⚠️ Use: !timeclockfix <entry_id> <clock_in_iso> <clock_out_iso> <reason>\n"
                "Example: !timeclockfix 42 2026-09-30T09:00:00-05:00 2026-09-30T17:00:00-05:00 Forgot to clock out"
            )
            return

        entry_id, clock_in, clock_out, reason = parts[1], parts[2], parts[3], parts[4]

        try:
            response = requests.post(
                f"{TIMECLOCK_API_URL}/api/timeclock/admin/entries/{entry_id}/adjust",
                headers={"x-timeclock-admin-secret": TIMECLOCK_ADMIN_SECRET},
                json={
                    "clockIn": clock_in,
                    "clockOut": clock_out,
                    "reason": reason,
                    "actor": str(message.author)
                },
                timeout=10
            )
            data = response.json() if response.content else {}

            if response.ok:
                entry = data.get("entry", {})
                await message.reply(
                    f"✅ Corrected entry {entry_id} for {entry.get('employeeName', 'employee')}. "
                    "The original values and reason were preserved in the audit trail."
                )
            else:
                await message.reply(f"❌ Could not correct entry: {data.get('error', response.text)}")
        except Exception as e:
            await message.reply(f"❌ Timeclock API error: {e}")
        return
    if message.content.startswith("!timeclockpin"):
        if not has_timeclock_admin_role(message.author):
            return

        parts = message.content.split()
        if len(parts) != 3 or not message.mentions:
            await message.reply("⚠️ Use: `!timeclockpin @Employee 1234`")
            return

        member = message.mentions[0]
        pin = parts[-1].strip()

        if not re.fullmatch(r"\d{4}", pin):
            await message.reply("⚠️ The timeclock PIN must be exactly 4 digits.")
            return

        if not TIMECLOCK_API_URL or not TIMECLOCK_ADMIN_SECRET:
            await message.reply("❌ Timeclock PIN administration is not configured on the bot.")
            return

        employee_name = member.display_name or member.name

        try:
            response = requests.post(
                f"{TIMECLOCK_API_URL}/api/timeclock/admin/assign-pin",
                headers={"x-timeclock-admin-secret": TIMECLOCK_ADMIN_SECRET},
                json={
                    "employeeName": employee_name,
                    "discordUserId": str(member.id),
                    "pin": pin
                },
                timeout=10
            )
            data = response.json() if response.content else {}

            if response.ok:
                await message.reply(
                    f"✅ Assigned a timeclock PIN to **{employee_name}**. "
                    "They can now clock in/out with either their employee name or the 4-digit PIN."
                )
            else:
                await message.reply(f"❌ Could not assign PIN: {data.get('error', response.text)}")
        except Exception as e:
            await message.reply(f"❌ Timeclock API error: `{e}`")
        return

    if message.content.startswith("!recover"):
        if not message.author.guild_permissions.administrator:
            await message.reply("❌ You do not have permission to run this command.")
            return

        parts = message.content.split()
        if len(parts) < 3:
            await message.reply("⚠️ **Format Error!**\nPlease use: `!recover <Channel_ID> <Message_ID_1> <Message_ID_2>`")
            return

        channel_id_str = parts[1]
        message_ids = parts[2:]

        if channel_id_str not in CHANNEL_MAP:
            await message.reply(f"❌ Channel `{channel_id_str}` is not listed in your bot's `CHANNEL_MAP`.")
            return

        target_channel = bot.get_channel(int(channel_id_str))
        if not target_channel:
            await message.reply("❌ I cannot see that channel. Check my permissions!")
            return

        destination = CHANNEL_MAP[channel_id_str]
        target_sheet_id = destination["sheet_id"]

        await message.reply(f"⏳ **Recovery started...** Scanning {len(message_ids)} posts. This may take a moment.")

        try:
            raw_sheet = client.open_by_key(target_sheet_id).worksheet("Raw Data")
            all_rows = raw_sheet.get_all_values()
        except Exception as e:
            await message.reply(f"❌ **Google Sheets Error:** Could not open 'Raw Data' tab.\n`{e}`")
            return

        existing_orders = set()
        user_timestamps = {}

        for r in all_rows[1:]: 
            if len(r) < 7: continue
            r_name, r_sku, r_stamp, r_id = r[0], r[1], r[5], r[6]
            
            existing_orders.add(f"{r_id}_{r_sku}")
            existing_orders.add(f"{r_name}_{r_sku}")

            if r_stamp:
                if r_id not in user_timestamps: user_timestamps[r_id] = r_stamp
                if r_name not in user_timestamps: user_timestamps[r_name] = r_stamp

        total_added = 0
        log_msgs = []

        for msg_id in message_ids:
            try:
                target_msg = await target_channel.fetch_message(int(msg_id))
            except Exception as e:
                log_msgs.append(f"❌ Post `{msg_id}`: Failed to fetch API.")
                continue

            product_name = target_msg.embeds[0].title if target_msg.embeds else "Unknown Product"
            full_text = target_msg.content or ""
            if target_msg.embeds:
                embed = target_msg.embeds[0]
                full_text += f" {embed.title or ''} {embed.description or ''}"
                if embed.footer: full_text += f" {embed.footer.text or ''}"
                for field in embed.fields:
                    full_text += f" {field.name} {field.value}"

            full_text = full_text.replace("*", "").replace("_", "")
            sku_match = re.search(r'SKU:\s*([A-Za-z0-9_-]+)', full_text, re.IGNORECASE)
            sku = sku_match.group(1) if sku_match else "NO_SKU"

            if not target_msg.reactions:
                log_msgs.append(f"⚠️ Post `{msg_id}`: No reactions found.")
                continue

            users_found = 0
            added_this_msg = 0

            for reaction in target_msg.reactions:
                ordered_users = []
                async for r_user in reaction.users():
                    if not r_user.bot:
                        ordered_users.append(r_user)
                        users_found += 1

                user_times = []
                for u in ordered_users:
                    u_id = str(u.id)
                    u_name = u.name
                    
                    stamp_str = user_timestamps.get(u_id) or user_timestamps.get(u_name)
                    dt_obj = None
                    
                    if stamp_str:
                        try:
                            dt_obj = datetime.strptime(stamp_str, "%Y-%m-%d %H:%M:%S")
                        except Exception:
                            dt_obj = None

                    user_times.append({"id": u_id, "name": u_name, "dt": dt_obj})

                for i, item in enumerate(user_times):
                    if item["dt"] is None:
                        prev_dt = None
                        for j in range(i - 1, -1, -1):
                            if user_times[j]["dt"] is not None:
                                prev_dt = user_times[j]["dt"]
                                break
                        
                        next_dt = None
                        for j in range(i + 1, len(user_times)):
                            if user_times[j]["dt"] is not None:
                                next_dt = user_times[j]["dt"]
                                break

                        if prev_dt and next_dt:
                            time_diff = (next_dt - prev_dt) / 2
                            item["dt"] = prev_dt + time_diff
                        elif prev_dt:
                            item["dt"] = prev_dt + timedelta(seconds=1)
                        elif next_dt:
                            item["dt"] = next_dt - timedelta(seconds=1)
                        else:
                            item["dt"] = target_msg.created_at.replace(tzinfo=timezone.utc)

                for item in user_times:
                    u_id = item["id"]
                    u_name = item["name"]
                    
                    if f"{u_id}_{sku}" in existing_orders or f"{u_name}_{sku}" in existing_orders:
                        continue

                    final_stamp = item["dt"].strftime("%Y-%m-%d %H:%M:%S")

                    raw_sheet.append_row([u_name, sku, product_name, "1", "Pending", final_stamp, u_id])
                    existing_orders.add(f"{u_id}_{sku}") 
                    total_added += 1
                    added_this_msg += 1

            log_msgs.append(f"✅ Post `{msg_id}` [SKU: {sku}] - Users Reacted: {users_found} | Recovered: {added_this_msg}")

        final_report = f"**🎉 Bulk Recovery Complete!**\nSuccessfully recovered `{total_added}` missing orders.\n\n**Diagnostics:**\n"
        for l in log_msgs:
            final_report += f"> {l}\n"

        await message.reply(final_report)


# ==========================================
# 6. RUN THE BOT
# ==========================================
bot_token = os.environ.get("DISCORD_TOKEN")
if bot_token:
    bot.run(bot_token)
else:
    print("ERROR: DISCORD_TOKEN environment variable is missing.")
