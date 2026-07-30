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
                            worksheet.delete_row(r_idx)
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
        
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

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
                            item["dt"] = target_msg.created_at

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
