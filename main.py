import os
import json
import discord
import gspread
import re
import asyncio
from discord.ext import commands, tasks
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta

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
    print("WARNING: GOOGLE_CREDENTIALS environment variable not found.")
    client = None

# ==========================================
# 2. BOT CONFIGURATION
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Load CHANNEL_MAP from environment variables
channel_map_env = os.environ.get("CHANNEL_SHEET_MAP")
if channel_map_env:
    CHANNEL_MAP = json.loads(channel_map_env)
else:
    # Fallback to local dict if environment variable is missing
    CHANNEL_MAP = {
        "1340074486783021169": {
            "sheet_id": "YOUR_SHEET_ID_HERE", 
            "tab_name": "Raw Data"
        }
    }

# --- GLOBAL MEMORY CACHE & QUEUE ---
MESSAGE_CACHE = {}      # Remembers SKUs to avoid Discord rate limits
SHEET_WRITE_QUEUE = []  # Holds orders to avoid Google Sheets API limits


# ==========================================
# 3. BACKGROUND ENGINE: BATCH UPLOADER
# ==========================================
@tasks.loop(seconds=15)
async def batch_write_to_sheets():
    global SHEET_WRITE_QUEUE
    
    # If there's nothing in the waiting room, do nothing
    if not SHEET_WRITE_QUEUE:
        return

    # Grab everything in the queue, then immediately clear it 
    batch_to_process = SHEET_WRITE_QUEUE[:]
    SHEET_WRITE_QUEUE.clear()

    # Group the orders by their target Sheet and Tab
    grouped_data = {}
    for item in batch_to_process:
        s_id = item["sheet_id"]
        t_name = item["tab_name"]
        
        if s_id not in grouped_data:
            grouped_data[s_id] = {}
        if t_name not in grouped_data[s_id]:
            grouped_data[s_id][t_name] = []
            
        grouped_data[s_id][t_name].append(item["row_data"])

    # Connect to Google Sheets ONCE per sheet and push all rows
    for s_id, tabs in grouped_data.items():
        try:
            sheet = client.open_by_key(s_id)
            for t_name, rows in tabs.items():
                worksheet = sheet.worksheet(t_name)
                
                # append_rows writes all orders in a single API call!
                worksheet.append_rows(rows)
                print(f"✅ BATCH SUCCESS: Uploaded {len(rows)} orders to '{t_name}'.")
                
        except Exception as e:
            print(f"❌ BATCH ERROR: Failed to upload to Google Sheets: {e}")
            # If Google fails (e.g. timeout), put orders back in the queue to try again
            SHEET_WRITE_QUEUE.extend(batch_to_process)


# ==========================================
# 4. BOT EVENTS & LISTENERS
# ==========================================
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    
    # Start the Google Sheets Batch engine
    if not batch_write_to_sheets.is_running():
        batch_write_to_sheets.start()
        print("✅ Batch upload engine started.")

@bot.event
async def on_raw_reaction_add(payload):
    if client is None:
        print("ERROR: Cannot write to Sheets. Google Auth failed on startup.")
        return

    channel_id_str = str(payload.channel_id)
    message_id_str = str(payload.message_id)

    # Filter: Only listen to channels defined in CHANNEL_MAP
    if channel_id_str not in CHANNEL_MAP:
        return

    user = await bot.fetch_user(payload.user_id)
    if user.bot: 
        return
        
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    destination = CHANNEL_MAP[channel_id_str]
    target_sheet_id = destination["sheet_id"]
    target_tab_name = destination["tab_name"]

    try:
        # --- DISCORD CACHE CHECK ---
        if message_id_str in MESSAGE_CACHE:
            product_name = MESSAGE_CACHE[message_id_str]["product"]
            sku = MESSAGE_CACHE[message_id_str]["sku"]
        else:
            channel = bot.get_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            
            product_name = message.embeds[0].title if message.embeds else "Unknown Product"
            
            # Omni-Scanner extraction
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

        # --- GOOGLE BATCH QUEUE DROP ---
        SHEET_WRITE_QUEUE.append({
            "sheet_id": target_sheet_id,
            "tab_name": target_tab_name,
            "row_data": [user.name, sku, product_name, "1", "Pending", timestamp, str(user.id)]
        })
        
        print(f"Queued: {user.name}'s order for {sku}. Waiting for batch upload...")
        
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

        # Read sheet and search backward for the most recent Pending order
        current_sheet = client.open_by_key(target_sheet_id).worksheet(target_tab_name)
        all_rows = current_sheet.get_all_values()

        row_to_delete = None
        
        for i in range(len(all_rows) - 1, 0, -1): 
            row = all_rows[i]
            if len(row) >= 5:
                if row[0] == user.name and row[1] == sku:
                    row_to_delete = i + 1 
                    break
        
        if row_to_delete:
            current_sheet.delete_rows(row_to_delete)
            print(f"Removed: {user.name}'s order for {sku} from {target_tab_name}.")

    except Exception as e:
        print(f"Error removing reaction: {e}")

# ==========================================
# 5. ADMIN COMMAND: BULK RECOVERY (WITH INTERPOLATION)
# ==========================================
@bot.event
async def on_message(message):
    if message.author.bot: 
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
                # 1. Gather all users in exact chronological order
                ordered_users = []
                async for r_user in reaction.users():
                    if not r_user.bot:
                        ordered_users.append(r_user)
                        users_found += 1

                # 2. Build a list of dictionaries with parsed datetime objects
                user_times = []
                for u in ordered_users:
                    u_id = str(u.id)
                    u_name = u.name
                    
                    stamp_str = user_timestamps.get(u_id) or user_timestamps.get(u_name)
                    dt_obj = None
                    
                    if stamp_str:
                        try:
                            # Convert string to math-able datetime object
                            dt_obj = datetime.strptime(stamp_str, "%Y-%m-%d %H:%M:%S")
                        except Exception:
                            dt_obj = None

                    user_times.append({"id": u_id, "name": u_name, "dt": dt_obj})

                # 3. INTERPOLATION ENGINE (The Math)
                for i, item in enumerate(user_times):
                    if item["dt"] is None:
                        # Find the closest known time BEFORE this user
                        prev_dt = None
                        for j in range(i - 1, -1, -1):
                            if user_times[j]["dt"] is not None:
                                prev_dt = user_times[j]["dt"]
                                break
                        
                        # Find the closest known time AFTER this user
                        next_dt = None
                        for j in range(i + 1, len(user_times)):
                            if user_times[j]["dt"] is not None:
                                next_dt = user_times[j]["dt"]
                                break

                        # Calculate the missing time!
                        if prev_dt and next_dt:
                            # Split the difference exactly down the middle
                            time_diff = (next_dt - prev_dt) / 2
                            item["dt"] = prev_dt + time_diff
                        elif prev_dt:
                            # End of the line, just add 1 second to the guy before them
                            item["dt"] = prev_dt + timedelta(seconds=1)
                        elif next_dt:
                            # Start of the line, just subtract 1 second from the guy after them
                            item["dt"] = next_dt - timedelta(seconds=1)
                        else:
                            # Total API Crash (Nobody has a timestamp). Use message creation.
                            item["dt"] = target_msg.created_at

                # 4. Push the interpolated data to Google Sheets
                for item in user_times:
                    u_id = item["id"]
                    u_name = item["name"]
                    
                    # Only append if they aren't already in the sheet
                    if f"{u_id}_{sku}" in existing_orders or f"{u_name}_{sku}" in existing_orders:
                        continue

                    # Convert the calculated datetime object back to a clean string
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
    print("ERROR: DISCORD_BOT_TOKEN environment variable is missing.")
