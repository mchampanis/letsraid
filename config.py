import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
LFG_CHANNEL_NAME = os.environ.get("LFG_CHANNEL_NAME", "looking-for-game")
DB_PATH = os.environ.get("DB_PATH", "letsraid.db")
