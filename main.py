import os
import json
import discord
import gspread
from discord.ext import commands
from oauth2client.service_account import ServiceAccountCredentials

# --- 1. SETUP GOOGLE AUTH ---
# Pull credentials from Railway Variables
creds_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
if not creds_str:
    print("CRITICAL WARNING: GOOGLE_SERVICE_ACCOUNT_JSON variable is missing!")
else:
    creds_dict = json.loads(creds_str)
    scope = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/spreadsheets', "https://www.googleapis.com/drive/api/v3", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)

# --- 2. LOAD YOUR CHANNEL MAP ---
# Pull the dynamic routing map from Railway Variables
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
    # JSON keys are always strings, so we convert the Discord ID to a string to check it
    channel_id_str = str(payload.channel_id)

    # 1. Filter: Check if this channel is in our Railway map
    if channel_id_str not in CHANNEL_MAP:
        return

    # 2. Filter: Only listen to the Anchor Emoji (📦)
    if payload.emoji.name != "📦":
        return

    # 3. Identify the user
    user = await bot.fetch_user(payload.user_id)
    if user.bot: 
        return
    
    # 4. Extract destination info from your Railway map
    destination = CHANNEL_MAP[channel_id_str]
    target_sheet_id = destination["sheet_id"]
    target_tab_name = destination["tab_name"]

    try:
        # Get the product name from the Discord message
        channel = bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        product_name = message.embeds[0].title if message.embeds else "Unknown Product"

        # Open the correct specific sheet & tab, and write the data
        current_sheet = client.open_by_key(target_sheet_id).worksheet(target_tab_name)
        current_sheet.append_row([user.name, product_name, "1", "Pending"])
        
        print(f"Success! Logged {user.name}'s order for {product_name} into {target_tab_name}.")
        
    except Exception as e:
        print(f"Error writing to Sheet: {e}")

# --- 5. SYSTEM COMMANDS & LAUNCH ---
@bot.event
async def on_ready():
    print(f'Successfully logged in as {bot.user} (ID: {bot.user.id})')
    print('System Online: Ready to route orders to Google Sheets.')
    print('------')

@bot.command(name="ping")
async def ping(ctx):
    await ctx.send("Pong! The bot is online, hosted on Railway, and ready for preorders.")

if __name__ == "__main__":
    # Fetch the secret token you added to the Railway dashboard
    token = os.environ.get("DISCORD_TOKEN")
    
    if not token:
        print("CRITICAL ERROR: DISCORD_TOKEN environment variable not found.")
        print("Please ensure it is set in the Railway Variables tab.")
    else:
        # Start the bot
        bot.run(token)
