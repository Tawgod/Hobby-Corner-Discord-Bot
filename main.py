import os
import json
import discord
import gspread
from discord.ext import commands
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime # Added for the Timestamp

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

# --- 4. THE REACTION LISTENER ---
@bot.event
async def on_raw_reaction_add(payload):
    channel_id_str = str(payload.channel_id)

    if channel_id_str not in CHANNEL_MAP:
        return
    if payload.emoji.name != "📦":
        return

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
        
        # 1. Try to get it from a Rich Embed first (Your Apps Script Posts)
        if message.embeds:
            embed = message.embeds[0]
            product_name = embed.title if embed.title else "Unknown Product"
            sku = embed.footer.text if embed.footer else "NO_SKU"
            
        # 2. Fallback: Try to get it from Plain Text
        else:
            text = message.content
            product_name = text.split('\n')[0] # Assumes the first line of your text is the product name
            
            # Simple word-search for "SKU:" in the plain text
            sku = "NO_SKU"
            words = text.split()
            for i, word in enumerate(words):
                # Look for SKU: or SKU
                if "SKU" in word.upper():
                    # Grab the very next word as the SKU
                    if i + 1 < len(words):
                        sku = words[i + 1]
                    break

        # Generate the Timestamp
        timestamp = datetime.now().strftime("%m/%d/%Y %I:%M:%S %p")

        # Push to Raw Data
        current_sheet = client.open_by_key(target_sheet_id).worksheet(target_tab_name)
        current_sheet.append_row([user.name, sku, product_name, "1", "Pending", timestamp])
        
        print(f"Success! Logged {user.name}'s order for [{sku}] {product_name}.")
        
    except Exception as e:
        print(f"Error writing to Sheet: {e}")

# --- 4.5. THE REACTION REMOVAL LISTENER (THE UNDO BUTTON) ---
@bot.event
async def on_raw_reaction_remove(payload):
    channel_id_str = str(payload.channel_id)

    # 1. Filters
    if channel_id_str not in CHANNEL_MAP:
        return
    if payload.emoji.name != "📦":
        return

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
        # 2. Fetch the SKU of the item they un-reacted to
        channel = bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        
        if message.embeds:
            sku = message.embeds[0].footer.text if message.embeds[0].footer else "NO_SKU"
        else:
            sku = "NO_SKU"

        # 3. Connect to the Sheet and pull all current records
        current_sheet = client.open_by_key(target_sheet_id).worksheet(target_tab_name)
        all_rows = current_sheet.get_all_values()

        # 4. Search backwards to find their most recent Pending order
        row_to_delete = None
        
        # Loop from the bottom of the sheet up to row 2 (skipping the header)
        for i in range(len(all_rows) - 1, 0, -1): 
            row = all_rows[i]
            
            # Check if row has enough columns: Discord(0), SKU(1), Status(4)
            if len(row) >= 5:
                if row[0] == user.name and row[1] == sku and row[4] == "Pending":
                    row_to_delete = i + 1 # Google Sheets rows are 1-indexed
                    break # Stop searching once we find the most recent match
        
        # 5. Execute the Deletion
        if row_to_delete:
            current_sheet.delete_rows(row_to_delete)
            print(f"Removed: Deleted {user.name}'s pending order for [{sku}].")
        else:
            print(f"Ignored: {user.name} removed a reaction, but no 'Pending' order was found for [{sku}].")
            
    except Exception as e:
        print(f"Error removing from Sheet: {e}")

# --- 5. SYSTEM COMMANDS ---
@bot.event
async def on_ready():
    print(f'Successfully logged in as {bot.user}')
    print('System Online: Ready to route SKUs and Timestamps to Google Sheets.')

@bot.command(name="ping")
async def ping(ctx):
    await ctx.send("Pong! The bot is online and tracking SKUs.")

if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("CRITICAL ERROR: DISCORD_TOKEN environment variable not found.")
    else:
        bot.run(token)
