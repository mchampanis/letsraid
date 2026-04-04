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

# Voice channels to hide from the VC picker, per guild.
# Format: HIDDEN_VC=guild_id:channel_id,channel_id;guild_id:channel_id
HIDDEN_VC: dict[int, set[int]] = {}
for _entry in os.environ.get("HIDDEN_VC", "").split(";"):
    _entry = _entry.strip()
    if not _entry or ":" not in _entry:
        continue
    _gid, _cids = _entry.split(":", 1)
    try:
        _gid_int = int(_gid.strip())
        _cid_set = {int(c.strip()) for c in _cids.split(",") if c.strip()}
    except ValueError:
        import logging as _logging
        _logging.getLogger("letsraid.config").warning("Malformed HIDDEN_VC entry, skipping: %r", _entry)
        continue
    if _cid_set:
        HIDDEN_VC[_gid_int] = _cid_set
