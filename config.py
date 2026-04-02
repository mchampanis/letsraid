import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
GUILD_IDS = [int(gid.strip()) for gid in os.environ["GUILD_IDS"].split(",")]
LFG_CHANNEL_NAME = os.environ.get("LFG_CHANNEL_NAME", "looking-for-game")
DB_PATH = os.environ.get("DB_PATH", "letsraid.db")

# Role names pinged per mode
LFG_ROLE_NAMES = {
    "pvp": os.environ.get("LFG_PVP_ROLE", "LFG PvP"),
    "pve": os.environ.get("LFG_PVE_ROLE", "LFG PvE"),
}

# Auto-move users to voice channel on join (true/false)
AUTO_JOIN_VC = os.environ.get("AUTO_JOIN_VC", "true").lower() == "true"
