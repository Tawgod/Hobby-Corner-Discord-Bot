import os
import json
import discord
import gspread
import re
from discord.ext import commands
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

# --- 1. SETUP GOOGLE AUTH ---
client = None 

creds_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
if not creds_str:
    print("CRITICAL ERROR: GOOGLE_SERVICE_ACCOUNT_JSON variable is missing!")
else:
    try:
        creds_dict = json.loads(creds_str)
        scope = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        print("Google Sheets Authenticated Successfully.")
    except Exception as e:
        print(f"Failed to authenticate with Google: {e}")

# --- LOCAL MEMORY CACHE ---
# This prevents Discord rate limits by remembering SKUs we've already looked up
MESSAGE_CACHE = {}

# --- 2. LOAD YOUR CHANNEL MAP ---
map_str = os.environ.get("CHANNEL_SHEET_MAP")
if not map_str:
    print("CRITICAL WARNING: CHANNEL_SHEET_MAP variable is missing!")
    CHANNEL_MAP = {}
else:
    CHANNEL_MAP = json.loads(map_str)

# --- 3. BOT INITIALIZATION ---
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
bot = commands.Bot(command_prefix="!", intents=intents)


# ==========================================
# 4. THE REACTION ADD LISTENER
# ==========================================
@bot.event
async def on_raw_reaction_add(payload):
    # Safety Check: Make sure Google Sheets authenticated correctly
    if client is None:
        print("ERROR: Cannot write to Sheets. Google Auth failed on startup.")
        return

    channel_id_str = str(payload.channel_id)
    message_id_str = str(payload.message_id)

    # 1. Filter: Check if this channel is in our Railway map
    if channel_id_str not in CHANNEL_MAP:
        return

    # 2. Identify the user
    user = await bot.fetch_user(payload.user_id)
    if user.bot: 
        return
        
    # Generate the exact timestamp of the click
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # 3. Extract destination info from your Railway map
    destination = CHANNEL_MAP[channel_id_str]
    target_sheet_id = destination["sheet_id"]
    target_tab_name = destination["tab_name"]

    try:
        # --- THE CACHE SYSTEM ---
        if message_id_str in MESSAGE_CACHE:
            # Pull from memory instantly (Zero Discord API calls!)
            product_name = MESSAGE_CACHE[message_id_str]["product"]
            sku = MESSAGE_CACHE[message_id_str]["sku"]
        else:
            # Fetch from Discord only this one time
            channel = bot.get_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            
            product_name = message.embeds[0].title if message.embeds else "Unknown Product"
            
            # --- THE OMNI-SCANNER ---
            full_text = ""
            if message.embeds:
                for embed in message.embeds:
                    full_text += str(embed.title) + " "
                    full_text += str(embed.description) + " "
                    if embed.footer:
                        full_text += str(embed.footer.text) + " "
                    for field in embed.fields:
                        full_text += str(field.name) + " " + str(field.value) + " "
            else:
                full_text = message.content
                
            full_text = full_text.replace("*", "").replace("_", "")
            sku_match = re.search(r'SKU:\s*([A-Za-z0-9_-]+)', full_text, re.IGNORECASE)
            sku = sku_match.group(1) if sku_match else "NO_SKU"

            # Save it to memory so we never ask Discord again
            MESSAGE_CACHE[message_id_str] = {
                "product": product_name,
                "sku": sku
            }

        # 4. Open the correct specific sheet & tab, and push the data
        current_sheet = client.open_by_key(target_sheet_id).worksheet(target_tab_name)
        
        # Format: [Discord Name, SKU, Item Description, Qty, Status, Timestamp, Discord ID]
        current_sheet.append_row([user.name, sku, product_name, "1", "Pending", timestamp, str(user.id)])
        
        print(f"Success! Logged {user.name}'s order for {sku} into {target_tab_name}.")
        
    except Exception as e:
        print(f"Error writing to Sheet: {e}")

# ==========================================
# 5. THE REACTION REMOVAL LISTENER (UNDO)
# ==========================================
@bot.event
async def on_raw_reaction_remove(payload):
    channel_id_str = str(payload.channel_id)

    # Filter: Only listen to mapped channels
    if channel_id_str not in CHANNEL_MAP:
        return
        
    # NOTE: The Emoji filter was removed here too!

    user = await bot.fetch_user(payload.user_id)
    if user.bot: 
        return
    
    destination = CHANNEL_MAP[channel_id_str]
    target_sheet_id = destination["sheet_id"]
    target_tab_name = destination["tab_name"]

    if client is None:
        print("Error: Bot is not authenticated with Google Sheets.")
        return

    try:
        channel = bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        
        sku = "NO_SKU"

        # Omni-Scanner to find the SKU of the item being removed
        if message.embeds:
            embed = message.embeds[0]
            all_text = f"{embed.title} {embed.description} "
            if embed.footer: all_text += f"{embed.footer.text} "
            if embed.author: all_text += f"{embed.author.name} "
            for field in embed.fields:
                all_text += f"{field.name} {field.value} "
            clean_text = all_text.replace('*', '').replace('_', '').replace('`', '')
            match = re.search(r"SKU\s*:?\s*([^\s]+)", clean_text, re.IGNORECASE)
            if match:
                sku = match.group(1)
        else:
            text = message.content
            if text:
                clean_text = text.replace('*', '').replace('_', '').replace('`', '')
                match = re.search(r"SKU\s*:?\s*([^\s]+)", clean_text, re.IGNORECASE)
                if match:
                    sku = match.group(1)

        # Pull records and search backward for the most recent Pending order
        current_sheet = client.open_by_key(target_sheet_id).worksheet(target_tab_name)
        all_rows = current_sheet.get_all_values()

        row_to_delete = None
        
        # Loop from bottom up
        for i in range(len(all_rows) - 1, 0, -1): 
            row = all_rows[i]
            
            # Check if row matches: Discord(0) and SKU(1) (Ignores Status)
            if len(row) >= 5:
                if row[0] == user.name and row[1] == sku:
                    row_to_delete = i + 1 
                    break
        
        # Execute Deletion
        if row_to_delete:
            current_sheet.delete_rows(row_to_delete)
            print(f"Removed: Deleted {user.name}'s pending order for [{sku}].")
        else:
            print(f"Ignored: {user.name} removed a reaction, but no 'Pending' order was found for [{sku}].")
            
    except Exception as e:
        print(f"Error removing from Sheet: {e}")


# ==========================================
# 6. SYSTEM COMMANDS
# ==========================================
@bot.event
async def on_ready():
    print(f'Successfully logged in as {bot.user}')
    print('System Online: Ready to route SKUs and Timestamps (Any Emoji) to Google Sheets.')

@bot.command(name="ping")
async def ping(ctx):
    await ctx.send("Pong! The bot is online, tracking SKUs, and listening to all emojis.")

if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("CRITICAL ERROR: DISCORD_TOKEN environment variable not found.")
    else:
        bot.run(token)
