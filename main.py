import discord
from discord.ext import commands
import os

# 1. Set up Intents (Permissions)
# This allows the bot to read messages in channels so it can respond to commands
intents = discord.Intents.default()
intents.message_content = True

# 2. Initialize the Bot
# We are setting the command prefix to "!" (e.g., !ping, !inventory)
bot = commands.Bot(command_prefix="!", intents=intents)

# 3. The "On Ready" Event
# This triggers automatically the moment the bot successfully connects to Discord
@bot.event
async def on_ready():
    print(f'Successfully logged in as {bot.user} (ID: {bot.user.id})')
    print('System Online: Ready to bridge SQL Server 2022 Express and Lightspeed.')
    print('------')

# 4. A Basic Test Command
# Type !ping in your Discord server to test the connection
@bot.command(name="ping")
async def ping(ctx):
    await ctx.send("Pong! The bot is online, hosted on Railway, and ready for development.")

# 5. The Launch Sequence
if __name__ == "__main__":
    # Fetch the secret token you added to the Railway dashboard
    token = os.environ.get("DISCORD_TOKEN")
    
    if not token:
        print("CRITICAL ERROR: DISCORD_TOKEN environment variable not found.")
        print("Please ensure it is set in the Railway Variables tab.")
    else:
        # Start the bot
        bot.run(token)

pip install gspread oauth2client
