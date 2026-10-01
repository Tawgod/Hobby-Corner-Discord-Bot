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
from zoneinfo import ZoneInfo

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
CUSTOMER_SYNC_API_URL = os.environ.get("CUSTOMER_SYNC_API_URL", "").rstrip("/")
CUSTOMER_SYNC_ADMIN_KEY = os.environ.get("CUSTOMER_SYNC_ADMIN_KEY", "")
MASTER_CUSTOMER_SHEET_ID = os.environ.get("MASTER_CUSTOMER_SHEET_ID", "").strip()
MASTER_CUSTOMER_TAB = os.environ.get("MASTER_CUSTOMER_TAB", "Customer").strip() or "Customer"

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

CENTRAL_TZ = ZoneInfo("America/Chicago")

def format_central(value):
    if not value:
        return "OPEN"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(CENTRAL_TZ)
        return local.strftime("%m/%d/%Y %-I:%M %p CT")
    except Exception:
        return str(value)

def format_local_time(value):
    if value is None:
        return ""
    text = str(value)
    try:
        parsed = datetime.strptime(text[:5], "%H:%M")
        return parsed.strftime("%-I:%M %p")
    except Exception:
        return text

def parse_central_datetime(value):
    text = str(value or "").strip()
    formats = [
        "%m/%d/%Y %I:%M %p",
        "%Y-%m-%d %I:%M %p",
        "%m/%d/%Y %I %p",
        "%Y-%m-%d %I %p",
    ]
    for fmt in formats:
        try:
            local = datetime.strptime(text, fmt).replace(tzinfo=CENTRAL_TZ)
            return local.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    raise ValueError("Use a Central time like 09/30/2026 9:00 AM.")

def parse_hhmm_12(value):
    text = str(value or "").strip()
    for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).strftime("%H:%M")
        except ValueError:
            pass
    raise ValueError("Use a time like 9:00 AM.")


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
        pto_label = "yes" if employee.get("pto_eligible") else "no"

        entries.append(
            f"**{employee.get('name')}**\n"
            f"> Employee ID: **{employee.get('id')}**\n"
            f"> Lightspeed ID: {lightspeed_label}\n"
            f"> Discord: {discord_label}\n"
            f"> PIN: {pin_label}\n"
            f"> PTO eligible: {pto_label}"
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
            f"In: {format_central(entry.get('clock_in'))}\n"
            f"Out: {format_central(entry.get('clock_out'))}\n"
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
    clock_in = format_central(entry.get("clock_in"))
    clock_out = format_central(entry.get("clock_out"))
    name = str(entry.get("employee_name") or "Employee")
    entry_id = str(entry.get("id"))
    label = f"#{entry_id} • {name} • {clock_in.replace(' CT', '')}"
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
                f"> Clock in: {format_central(entry.get('clock_in'))}\n"
                f"> Clock out: {format_central(entry.get('clock_out')) if entry.get('clock_out') else 'OPEN'}\n"
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
            label="Clock In — Central",
            placeholder="09/30/2026 9:00 AM",
            required=True,
            default=format_central(entry.get("clock_in")).replace(" CT", "") if entry.get("clock_in") else ""
        )
        self.clock_out_input = discord.ui.TextInput(
            label="Clock Out — Central",
            placeholder="09/30/2026 5:00 PM",
            required=True,
            default=format_central(entry.get("clock_out")).replace(" CT", "") if entry.get("clock_out") else ""
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

        try:
            clock_in_iso = parse_central_datetime(str(self.clock_in_input))
            clock_out_iso = parse_central_datetime(str(self.clock_out_input))
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        entry_id = str(self.entry_id_input).strip()

        try:
            response = requests.post(
                f"{TIMECLOCK_API_URL}/api/timeclock/admin/entries/{entry_id}/adjust",
                headers=timeclock_headers(),
                json={
                    "clockIn": clock_in_iso,
                    "clockOut": clock_out_iso,
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


class ManualEntryModal(discord.ui.Modal, title="Create Missed Time Entry"):
    employee_id = discord.ui.TextInput(label="Employee ID", required=True, max_length=20)
    clock_in = discord.ui.TextInput(
        label="Clock In — Central",
        placeholder="09/30/2026 9:00 AM",
        required=True
    )
    clock_out = discord.ui.TextInput(
        label="Clock Out — Central",
        placeholder="09/30/2026 5:00 PM",
        required=True
    )
    reason = discord.ui.TextInput(
        label="Reason",
        placeholder="Employee forgot to clock in",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_timeclock_admin(interaction):
            return
        try:
            clock_in_iso = parse_central_datetime(str(self.clock_in))
            clock_out_iso = parse_central_datetime(str(self.clock_out))
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            response = requests.post(
                f"{TIMECLOCK_API_URL}/api/timeclock/admin/entries",
                headers=timeclock_headers(),
                json={
                    "employeeId": str(self.employee_id).strip(),
                    "clockIn": clock_in_iso,
                    "clockOut": clock_out_iso,
                    "reason": str(self.reason).strip(),
                    "actor": str(interaction.user)
                },
                timeout=10
            )
            data = response.json() if response.content else {}
            if response.ok:
                entry = data.get("entry", {})
                await interaction.followup.send(
                    f"Created **Entry ID {entry.get('id')}** for **{entry.get('employeeName')}**\n"
                    f"{format_central(entry.get('clock_in'))} → {format_central(entry.get('clock_out'))}",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"Could not create entry: {data.get('error', response.text)}",
                    ephemeral=True
                )
        except Exception as e:
            await interaction.followup.send(f"Timeclock API error: {e}", ephemeral=True)


class TimeOffRequestModal(discord.ui.Modal, title="Request Time Off"):
    start_date = discord.ui.TextInput(label="Start Date", placeholder="YYYY-MM-DD", required=True, max_length=10)
    end_date = discord.ui.TextInput(label="End Date", placeholder="YYYY-MM-DD", required=True, max_length=10)
    use_pto = discord.ui.TextInput(label="Use PTO? yes/no", placeholder="yes", required=True, max_length=3)
    pto_hours = discord.ui.TextInput(
        label="PTO Hours (required if yes)",
        placeholder="8",
        required=False,
        max_length=8
    )
    reason = discord.ui.TextInput(label="Reason (optional)", required=False, style=discord.TextStyle.paragraph, max_length=300)

    async def on_submit(self, interaction: discord.Interaction):
        start = str(self.start_date).strip()
        end = str(self.end_date).strip()
        use_pto_text = str(self.use_pto).strip().lower()
        pto_hours_text = str(self.pto_hours).strip()

        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
            await interaction.response.send_message("Dates must be YYYY-MM-DD.", ephemeral=True)
            return
        if use_pto_text not in ("yes", "no"):
            await interaction.response.send_message("Use PTO must be yes or no.", ephemeral=True)
            return
        if use_pto_text == "yes":
            try:
                if float(pto_hours_text) <= 0:
                    raise ValueError
            except ValueError:
                await interaction.response.send_message(
                    "Enter how many PTO hours you want to use.",
                    ephemeral=True
                )
                return

        await interaction.response.defer(ephemeral=True)
        try:
            response = requests.post(
                f"{TIMECLOCK_API_URL}/api/timeclock/time-off/request",
                headers=timeclock_headers(),
                json={
                    "discordUserId": str(interaction.user.id),
                    "startDate": start,
                    "endDate": end,
                    "usePto": use_pto_text == "yes",
                    "ptoHours": pto_hours_text if use_pto_text == "yes" else None,
                    "reason": str(self.reason).strip()
                },
                timeout=10
            )
            data = response.json() if response.content else {}
            if response.ok:
                request = data.get("request", {})
                if request.get("use_pto"):
                    pto_text = f"using **{request.get('pto_hours')} PTO hours**"
                else:
                    pto_text = "as **unpaid time off**"

                await interaction.followup.send(
                    f"Time-off request **#{request.get('id')}** submitted for "
                    f"**{request.get('start_date')} through {request.get('end_date')}**, {pto_text}.",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"Could not submit request: {data.get('error', response.text)}",
                    ephemeral=True
                )
        except Exception as e:
            await interaction.followup.send(f"Timeclock API error: {e}", ephemeral=True)


class TimeOffDecisionView(discord.ui.View):
    def __init__(self, request):
        super().__init__(timeout=180)
        self.request = request

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await self._review(interaction, "APPROVED")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await self._review(interaction, "DENIED")

    @discord.ui.button(label="Approve + Post Shift", style=discord.ButtonStyle.primary)
    async def approve_shift(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.send_modal(CoverageShiftModal(self.request))

    async def _review(self, interaction, decision):
        await interaction.response.defer(ephemeral=True)
        response = requests.post(
            f"{TIMECLOCK_API_URL}/api/timeclock/admin/time-off/{self.request.get('id')}/review",
            headers=timeclock_headers(),
            json={"status": decision, "actor": str(interaction.user)},
            timeout=10
        )
        data = response.json() if response.content else {}
        if response.ok:
            await interaction.followup.send(
                f"Request **#{self.request.get('id')}** is now **{decision.lower()}**.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"Could not review request: {data.get('error', response.text)}",
                ephemeral=True
            )


class CoverageShiftModal(discord.ui.Modal, title="Approve and Post Coverage Shift"):
    shift_date = discord.ui.TextInput(label="Shift Date", placeholder="YYYY-MM-DD", required=True, max_length=10)
    start_time = discord.ui.TextInput(label="Start Time — Central", placeholder="9:00 AM", required=True, max_length=10)
    end_time = discord.ui.TextInput(label="End Time — Central", placeholder="5:00 PM", required=True, max_length=10)
    manager_note = discord.ui.TextInput(label="Manager Note (optional)", required=False, max_length=300)

    def __init__(self, request):
        super().__init__()
        self.request = request
        self.shift_date.default = str(request.get("start_date") or "")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            start_24 = parse_hhmm_12(str(self.start_time))
            end_24 = parse_hhmm_12(str(self.end_time))
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        response = requests.post(
            f"{TIMECLOCK_API_URL}/api/timeclock/admin/time-off/{self.request.get('id')}/review",
            headers=timeclock_headers(),
            json={
                "status": "APPROVED",
                "actor": str(interaction.user),
                "managerNote": str(self.manager_note).strip(),
                "postOpenShift": True,
                "shiftDate": str(self.shift_date).strip(),
                "startTime": start_24,
                "endTime": end_24
            },
            timeout=10
        )
        data = response.json() if response.content else {}
        if response.ok:
            shift = data.get("openShift") or {}
            await interaction.followup.send(
                f"Approved request **#{self.request.get('id')}** and posted open shift "
                f"**#{shift.get('id')}** for {shift.get('shift_date')} "
                f"{format_local_time(shift.get('start_time'))}–{format_local_time(shift.get('end_time'))}.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"Could not approve request: {data.get('error', response.text)}",
                ephemeral=True
            )


class TimeOffSelect(discord.ui.Select):
    def __init__(self, requests_list):
        self.request_map = {str(r.get("id")): r for r in requests_list}
        options = []
        for request in requests_list[:25]:
            pto = "PTO" if request.get("use_pto") else "No PTO"
            options.append(discord.SelectOption(
                label=f"#{request.get('id')} • {request.get('employee_name')}"[:100],
                value=str(request.get("id")),
                description=f"{request.get('start_date')} → {request.get('end_date')} • {pto}"[:100]
            ))
        super().__init__(placeholder="Select a time-off request", options=options)

    async def callback(self, interaction: discord.Interaction):
        request = self.request_map.get(self.values[0])
        await interaction.response.send_message(
            f"**Request #{request.get('id')} — {request.get('employee_name')}**\n"
            f"{request.get('start_date')} through {request.get('end_date')}\n"
            f"PTO: {'Yes' if request.get('use_pto') else 'No'}"
            + (f" ({request.get('pto_hours')} hours)" if request.get('pto_hours') else "")
            + f"\nReason: {request.get('reason') or 'None'}",
            view=TimeOffDecisionView(request),
            ephemeral=True
        )


class TimeOffSelectView(discord.ui.View):
    def __init__(self, requests_list):
        super().__init__(timeout=180)
        self.add_item(TimeOffSelect(requests_list))


async def send_pending_time_off(interaction):
    response = requests.get(
        f"{TIMECLOCK_API_URL}/api/timeclock/admin/time-off",
        headers=timeclock_headers(),
        params={"status": "PENDING"},
        timeout=10
    )
    data = response.json() if response.content else {}
    if not response.ok:
        await interaction.followup.send(f"Could not load requests: {data.get('error', response.text)}", ephemeral=True)
        return
    requests_list = data.get("requests", [])
    if not requests_list:
        await interaction.followup.send("No pending time-off requests.", ephemeral=True)
        return
    await interaction.followup.send(
        f"**Pending Time-Off Requests: {len(requests_list)}**\nSelect one to review.",
        view=TimeOffSelectView(requests_list[:25]),
        ephemeral=True
    )


async def announce_open_shift(interaction, shift, role=None):
    if role is None:
        return

    mention = role.mention
    note = shift.get("note")
    note_text = f"\n{note}" if note else ""
    message = (
        f"{mention} **Open shift available**\n"
        f"📅 {shift.get('shift_date')}\n"
        f"🕒 {format_local_time(shift.get('start_time'))}–{format_local_time(shift.get('end_time'))} CT"
        f"{note_text}\n"
        "Use **/hc timeclock → Open Shifts** to claim it."
    )

    try:
        await interaction.channel.send(
            message,
            allowed_mentions=discord.AllowedMentions(roles=True)
        )
    except Exception as e:
        await interaction.followup.send(
            f"The shift was created, but I could not post the role notification: {e}",
            ephemeral=True
        )


class OpenShiftModal(discord.ui.Modal, title="Post Open Shift"):
    shift_date = discord.ui.TextInput(label="Shift Date", placeholder="YYYY-MM-DD", required=True, max_length=10)
    start_time = discord.ui.TextInput(label="Start Time — Central", placeholder="9:00 AM", required=True, max_length=10)
    end_time = discord.ui.TextInput(label="End Time — Central", placeholder="5:00 PM", required=True, max_length=10)
    note = discord.ui.TextInput(label="Note (optional)", placeholder="Extra hours available", required=False, max_length=300)

    def __init__(self, notification_role=None):
        super().__init__()
        self.notification_role = notification_role

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_timeclock_admin(interaction):
            return

        try:
            start_24 = parse_hhmm_12(str(self.start_time))
            end_24 = parse_hhmm_12(str(self.end_time))
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        response = requests.post(
            f"{TIMECLOCK_API_URL}/api/timeclock/admin/shifts",
            headers=timeclock_headers(),
            json={
                "employeeId": None,
                "shiftDate": str(self.shift_date).strip(),
                "startTime": start_24,
                "endTime": end_24,
                "open": True,
                "note": str(self.note).strip(),
                "notificationRoleId": str(self.notification_role.id) if self.notification_role else None,
                "actor": str(interaction.user)
            },
            timeout=10
        )
        data = response.json() if response.content else {}

        if response.ok:
            shift = data.get("shift", {})
            notify_text = (
                f" and notified **{self.notification_role.name}**"
                if self.notification_role else ""
            )
            await interaction.followup.send(
                f"Posted **open shift #{shift.get('id')}** for {shift.get('shift_date')} "
                f"{format_local_time(shift.get('start_time'))}–{format_local_time(shift.get('end_time'))} CT"
                f"{notify_text}.",
                ephemeral=True
            )
            await announce_open_shift(interaction, shift, self.notification_role)
        else:
            await interaction.followup.send(
                f"Could not create shift: {data.get('error', response.text)}",
                ephemeral=True
            )


class ShiftNotifyRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(
            placeholder="Choose a role to notify",
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        if not await require_timeclock_admin(interaction):
            return
        role = self.values[0]
        await interaction.response.send_modal(OpenShiftModal(notification_role=role))


class OpenShiftNotifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(ShiftNotifyRoleSelect())

    @discord.ui.button(label="No Notification", style=discord.ButtonStyle.secondary, row=1)
    async def no_notification(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.send_modal(OpenShiftModal())


class ShiftCreateModeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Assigned Shift", style=discord.ButtonStyle.secondary, emoji="👤")
    async def assigned_shift(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.send_modal(CreateShiftModal())

    @discord.ui.button(label="Open / Extra Hours", style=discord.ButtonStyle.success, emoji="📣")
    async def open_shift(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.send_message(
            "Choose a Discord role to notify, or post the open shift without a notification.",
            view=OpenShiftNotifyView(),
            ephemeral=True
        )


class CreateShiftModal(discord.ui.Modal, title="Create Assigned Shift"):
    employee = discord.ui.TextInput(label="Employee ID", placeholder="12", required=True, max_length=20)
    shift_date = discord.ui.TextInput(label="Shift Date", placeholder="YYYY-MM-DD", required=True, max_length=10)
    start_time = discord.ui.TextInput(label="Start Time — Central", placeholder="9:00 AM", required=True, max_length=10)
    end_time = discord.ui.TextInput(label="End Time — Central", placeholder="5:00 PM", required=True, max_length=10)
    note = discord.ui.TextInput(label="Note (optional)", required=False, max_length=300)

    async def on_submit(self, interaction: discord.Interaction):
        employee_value = str(self.employee).strip()
        try:
            start_24 = parse_hhmm_12(str(self.start_time))
            end_24 = parse_hhmm_12(str(self.end_time))
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        if not employee_value.isdigit():
            await interaction.response.send_message("Employee ID must be a number.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        response = requests.post(
            f"{TIMECLOCK_API_URL}/api/timeclock/admin/shifts",
            headers=timeclock_headers(),
            json={
                "employeeId": employee_value,
                "shiftDate": str(self.shift_date).strip(),
                "startTime": start_24,
                "endTime": end_24,
                "open": False,
                "note": str(self.note).strip(),
                "actor": str(interaction.user)
            },
            timeout=10
        )
        data = response.json() if response.content else {}
        if response.ok:
            shift = data.get("shift", {})
            await interaction.followup.send(
                f"Created **assigned shift #{shift.get('id')}** "
                f"for {shift.get('shift_date')} "
                f"{format_local_time(shift.get('start_time'))}–{format_local_time(shift.get('end_time'))}.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(f"Could not create shift: {data.get('error', response.text)}", ephemeral=True)


class OpenShiftSelect(discord.ui.Select):
    def __init__(self, shifts):
        self.shift_map = {str(s.get("id")): s for s in shifts}
        options = []
        for shift in shifts[:25]:
            options.append(discord.SelectOption(
                label=f"Shift #{shift.get('id')} • {shift.get('shift_date')}"[:100],
                value=str(shift.get("id")),
                description=f"{format_local_time(shift.get('start_time'))}–{format_local_time(shift.get('end_time'))} • {shift.get('note') or 'Open shift'}"[:100]
            ))
        super().__init__(placeholder="Select an open shift to claim", options=options)

    async def callback(self, interaction: discord.Interaction):
        shift = self.shift_map.get(self.values[0])
        await interaction.response.defer(ephemeral=True)
        response = requests.post(
            f"{TIMECLOCK_API_URL}/api/timeclock/shifts/{shift.get('id')}/claim",
            headers=timeclock_headers(),
            json={"discordUserId": str(interaction.user.id), "actor": str(interaction.user)},
            timeout=10
        )
        data = response.json() if response.content else {}
        if response.ok:
            await interaction.followup.send(
                f"You claimed **shift #{shift.get('id')}** on {shift.get('shift_date')} "
                f"{format_local_time(shift.get('start_time'))}–{format_local_time(shift.get('end_time'))}.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(f"Could not claim shift: {data.get('error', response.text)}", ephemeral=True)


class OpenShiftView(discord.ui.View):
    def __init__(self, shifts):
        super().__init__(timeout=180)
        self.add_item(OpenShiftSelect(shifts))


async def send_open_shifts(interaction):
    response = requests.get(
        f"{TIMECLOCK_API_URL}/api/timeclock/shifts",
        headers=timeclock_headers(),
        params={"openOnly": "true"},
        timeout=10
    )
    data = response.json() if response.content else {}
    if not response.ok:
        await interaction.followup.send(f"Could not load shifts: {data.get('error', response.text)}", ephemeral=True)
        return
    shifts = data.get("shifts", [])
    if not shifts:
        await interaction.followup.send("There are no open shifts right now.", ephemeral=True)
        return
    await interaction.followup.send(
        "**Open Shifts**\nSelect one to claim it.",
        view=OpenShiftView(shifts[:25]),
        ephemeral=True
    )


class StoreHoursModal(discord.ui.Modal, title="Set Store Hours"):
    day = discord.ui.TextInput(label="Day (0=Sun … 6=Sat)", placeholder="1", required=True, max_length=1)
    closed = discord.ui.TextInput(label="Closed? yes/no", placeholder="no", required=True, max_length=3)
    open_time = discord.ui.TextInput(label="Open Time — Central", placeholder="10:00 AM", required=False, max_length=10)
    close_time = discord.ui.TextInput(label="Close Time — Central", placeholder="8:00 PM", required=False, max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        closed_text = str(self.closed).strip().lower()
        if not str(self.day).strip().isdigit() or int(str(self.day).strip()) not in range(7):
            await interaction.response.send_message("Day must be 0 through 6, Sunday through Saturday.", ephemeral=True)
            return
        if closed_text not in ("yes", "no"):
            await interaction.response.send_message("Closed must be yes or no.", ephemeral=True)
            return

        is_closed = closed_text == "yes"
        try:
            open_24 = None if is_closed else parse_hhmm_12(str(self.open_time))
            close_24 = None if is_closed else parse_hhmm_12(str(self.close_time))
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        response = requests.put(
            f"{TIMECLOCK_API_URL}/api/timeclock/admin/store-hours/{str(self.day).strip()}",
            headers=timeclock_headers(),
            json={
                "isClosed": is_closed,
                "openTime": open_24,
                "closeTime": close_24,
                "actor": str(interaction.user)
            },
            timeout=10
        )
        data = response.json() if response.content else {}
        if response.ok:
            hours = data.get("hours", {})
            day_names = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"]
            if hours.get("is_closed"):
                detail = "Closed"
            else:
                detail = f"{format_local_time(hours.get('open_time'))}–{format_local_time(hours.get('close_time'))}"
            await interaction.followup.send(
                f"Saved **{day_names[int(str(self.day).strip())]}** store hours: **{detail}**.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(f"Could not save store hours: {data.get('error', response.text)}", ephemeral=True)


class PtoEligibilityChoiceView(discord.ui.View):
    def __init__(self, employee):
        super().__init__(timeout=120)
        self.employee = employee

    async def set_eligibility(self, interaction, eligible):
        if not await require_timeclock_admin(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        employee_id = str(self.employee.get("id"))

        try:
            response = requests.post(
                f"{TIMECLOCK_API_URL}/api/timeclock/admin/employees/{employee_id}/pto-eligibility",
                headers=timeclock_headers(),
                json={
                    "eligible": eligible,
                    "actor": str(interaction.user)
                },
                timeout=10
            )
            data = response.json() if response.content else {}

            if response.ok:
                await interaction.followup.send(
                    f"Set **{data.get('employeeName')}** PTO eligibility to "
                    f"**{'Yes' if data.get('ptoEligible') else 'No'}**.",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"Could not update PTO eligibility: {data.get('error', response.text)}",
                    ephemeral=True
                )
        except Exception as e:
            await interaction.followup.send(f"Timeclock API error: {e}", ephemeral=True)

    @discord.ui.button(label="PTO Eligible", style=discord.ButtonStyle.success, emoji="✅")
    async def eligible_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.set_eligibility(interaction, True)

    @discord.ui.button(label="Not Eligible", style=discord.ButtonStyle.secondary, emoji="🚫")
    async def not_eligible_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.set_eligibility(interaction, False)


class PtoEmployeeSelect(discord.ui.Select):
    def __init__(self, employees):
        self.employee_map = {str(employee.get("id")): employee for employee in employees}
        options = []
        for employee in employees:
            status = "PTO eligible" if employee.get("pto_eligible") else "Not PTO eligible"
            options.append(
                discord.SelectOption(
                    label=str(employee.get("name") or f"Employee {employee.get('id')}")[:100],
                    value=str(employee.get("id")),
                    description=f"Employee ID {employee.get('id')} • {status}"[:100]
                )
            )

        super().__init__(
            placeholder="Choose an employee",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        if not await require_timeclock_admin(interaction):
            return

        employee = self.employee_map.get(self.values[0])
        if not employee:
            await interaction.response.send_message("Employee not found.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"**{employee.get('name')}** is currently "
            f"**{'PTO eligible' if employee.get('pto_eligible') else 'not PTO eligible'}**.\n"
            "Choose the new setting:",
            view=PtoEligibilityChoiceView(employee),
            ephemeral=True
        )


class PtoEmployeeSelectView(discord.ui.View):
    def __init__(self, employees):
        super().__init__(timeout=180)
        self.add_item(PtoEmployeeSelect(employees))


async def send_pto_employee_picker(interaction):
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
        await interaction.followup.send("No employees are available.", ephemeral=True)
        return

    chunks = [employees[i:i + 25] for i in range(0, len(employees), 25)]
    for index, chunk in enumerate(chunks, start=1):
        label = "Choose an employee to change PTO eligibility."
        if len(chunks) > 1:
            label += f" Page {index}/{len(chunks)}."
        await interaction.followup.send(
            label,
            view=PtoEmployeeSelectView(chunk),
            ephemeral=True
        )


class TimeclockControlPanel(discord.ui.View):
    ADMIN_LABELS = {
        "Employees", "Link Employee", "Reset PIN", "Review Queue",
        "Browse Entries", "Fix Entry", "Create Missed Entry",
        "Time-Off Requests", "Create Shift", "Store Hours", "PTO Eligibility"
    }

    def __init__(self, is_admin: bool):
        super().__init__(timeout=300)
        self.is_admin = is_admin
        if not is_admin:
            for item in self.children:
                if getattr(item, "label", None) in self.ADMIN_LABELS:
                    item.disabled = True

    @discord.ui.button(label="Request Time Off", style=discord.ButtonStyle.primary, emoji="🏖️", row=0)
    async def request_time_off(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TimeOffRequestModal())

    @discord.ui.button(label="Open Shifts", style=discord.ButtonStyle.primary, emoji="🙋", row=0)
    async def open_shifts(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            await send_open_shifts(interaction)
        except Exception as e:
            await interaction.followup.send(f"Timeclock API error: {e}", ephemeral=True)

    @discord.ui.button(label="Employees", style=discord.ButtonStyle.secondary, emoji="👥", row=1)
    async def employees_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await send_employee_list(interaction)
        except Exception as e:
            await interaction.followup.send(f"Timeclock API error: {e}", ephemeral=True)

    @discord.ui.button(label="Link Employee", style=discord.ButtonStyle.success, emoji="🔗", row=1)
    async def link_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.send_message("Choose the Discord member to link:", view=LinkMemberView(), ephemeral=True)

    @discord.ui.button(label="Reset PIN", style=discord.ButtonStyle.secondary, emoji="🔢", row=1)
    async def pin_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.send_message("Choose the linked employee:", view=ResetPinMemberView(), ephemeral=True)

    @discord.ui.button(label="Browse Entries", style=discord.ButtonStyle.secondary, emoji="📋", row=2)
    async def browse_entries_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.send_message("Choose how you want to find a timeclock entry:", view=EntryBrowserView(), ephemeral=True)

    @discord.ui.button(label="Fix Entry", style=discord.ButtonStyle.danger, emoji="🛠️", row=2)
    async def fix_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.send_modal(FixEntryModal())

    @discord.ui.button(label="Create Missed Entry", style=discord.ButtonStyle.success, emoji="➕", row=2)
    async def manual_entry(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.send_modal(ManualEntryModal())

    @discord.ui.button(label="Review Queue", style=discord.ButtonStyle.secondary, emoji="⚠️", row=2)
    async def review_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await send_review_queue(interaction)
        except Exception as e:
            await interaction.followup.send(f"Timeclock API error: {e}", ephemeral=True)

    @discord.ui.button(label="Time-Off Requests", style=discord.ButtonStyle.secondary, emoji="✅", row=3)
    async def time_off_requests(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await send_pending_time_off(interaction)
        except Exception as e:
            await interaction.followup.send(f"Timeclock API error: {e}", ephemeral=True)

    @discord.ui.button(label="Create Shift", style=discord.ButtonStyle.success, emoji="🗓️", row=3)
    async def create_shift(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.send_message(
            "Create an assigned shift, or post open/additional hours for staff to claim.",
            view=ShiftCreateModeView(),
            ephemeral=True
        )

    @discord.ui.button(label="Store Hours", style=discord.ButtonStyle.secondary, emoji="🏪", row=3)
    async def store_hours(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.send_modal(StoreHoursModal())

    @discord.ui.button(label="PTO Eligibility", style=discord.ButtonStyle.secondary, emoji="💼", row=3)
    async def pto_eligibility(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_timeclock_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await send_pto_employee_picker(interaction)
        except Exception as e:
            await interaction.followup.send(f"Timeclock API error: {e}", ephemeral=True)



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
        "Use the staff tools at the top, or the management tools below."
        if is_admin
        else "Use the buttons below to request time off or view open shifts."
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



def normalize_discord_match(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lstrip("@")).casefold()

def discord_aliases(value):
    text = str(value or "")
    aliases = []
    for part in re.split(r"[,;/|]+", text):
        normalized = normalize_discord_match(part)
        if normalized:
            aliases.append(normalized)
    return list(dict.fromkeys(aliases))

async def sync_master_customer_list_once():
    if client is None or not MASTER_CUSTOMER_SHEET_ID:
        return
    if not CUSTOMER_SYNC_API_URL or not CUSTOMER_SYNC_ADMIN_KEY:
        print("WARNING: Customer sync Railway endpoint is not configured.")
        return

    sheet = client.open_by_key(MASTER_CUSTOMER_SHEET_ID)
    worksheet = sheet.worksheet(MASTER_CUSTOMER_TAB)
    rows = worksheet.get_all_values()
    if not rows:
        return

    headers = {str(v).strip().casefold(): i for i, v in enumerate(rows[0])}
    required = ["name", "phone", "discord", "email", "discord id"]
    missing = [h for h in required if h not in headers]
    if missing:
        print(f"WARNING: Master Customer List missing columns: {missing}")
        return

    member_by_id = {}
    members_by_name = {}
    for guild in bot.guilds:
        for member in guild.members:
            if member.bot:
                continue
            member_by_id[str(member.id)] = member
            candidates = {
                normalize_discord_match(member.name),
                normalize_discord_match(member.display_name),
                normalize_discord_match(getattr(member, "global_name", None)),
            }
            for candidate in candidates:
                if not candidate:
                    continue
                members_by_name.setdefault(candidate, {})
                members_by_name[candidate][str(member.id)] = member

    id_updates = []
    payload = []
    discord_id_col = headers["discord id"] + 1

    for row_number, row in enumerate(rows[1:], start=2):
        def cell(header):
            idx = headers[header]
            return row[idx].strip() if idx < len(row) else ""

        name = cell("name")
        phone = cell("phone")
        discord_sheet_name = cell("discord")
        email = cell("email")
        discord_user_id = cell("discord id")

        member = None
        if discord_user_id:
            member = member_by_id.get(discord_user_id)
        elif discord_sheet_name:
            matches = {}
            for alias in discord_aliases(discord_sheet_name):
                for uid, candidate_member in members_by_name.get(alias, {}).items():
                    matches[uid] = candidate_member
            if len(matches) == 1:
                discord_user_id, member = next(iter(matches.items()))
                id_updates.append(gspread.Cell(row_number, discord_id_col, discord_user_id))

        payload.append({
            "row_number": row_number,
            "name": name,
            "phone": phone,
            "email": email,
            "discord_sheet_name": discord_sheet_name,
            "discord_user_id": discord_user_id,
            "discord_server_name": member.display_name if member else "",
            "discord_username": member.name if member else "",
        })

    if id_updates:
        worksheet.update_cells(id_updates, value_input_option="RAW")
        print(f"✅ Master Customer List: filled {len(id_updates)} unambiguous Discord ID(s).")

    response = requests.post(
        f"{CUSTOMER_SYNC_API_URL}/admin/customer-source/discord-sheet",
        headers={"x-admin-key": CUSTOMER_SYNC_ADMIN_KEY},
        json={"customers": payload},
        timeout=30
    )
    if not response.ok:
        raise RuntimeError(f"Customer sync API returned {response.status_code}: {response.text}")

    data = response.json()
    print(f"✅ Master Customer List sync: {data.get('customers_synced', 0)} customer row(s) sent to Railway.")

@tasks.loop(hours=1)
async def sync_master_customer_list_hourly():
    try:
        await sync_master_customer_list_once()
    except Exception as e:
        print(f"❌ Master Customer List hourly sync failed: {e}")

@sync_master_customer_list_hourly.before_loop
async def before_master_customer_sync():
    await bot.wait_until_ready()

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

    if not sync_master_customer_list_hourly.is_running():
        sync_master_customer_list_hourly.start()
        print("✅ Hourly Master Customer List sync started.")

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
