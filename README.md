# LetsRaid

Discord LFG (Looking For Game) bot for the First Wave Survivors server. Built for Arc Raiders (2-3 player teams).

## Features

- `/lfg` slash command with PvP/PvE mode selection and popup form
- Voice channel picker in the modal, auto-defaults to your current or least-full VC
- PvP/PvE mode icons on embeds
- Role pings to notify players looking for games
- Owner gets a DM with controls to finish the game, remove players, or change VC
- `/lfglist` to browse all open LFG posts
- `/lfgsetup` posts role picker and live game board in one command
- `/lfgstatus` personal role toggle with live-updating buttons
- Auto-move to voice channel on join (configurable)
- Voice channel awareness:
  - VC status auto-clears once the game has actually been played (creator in VC 10+ min, then VC empties)
  - Posts where the creator + at least one other player spent 1+ hour together in VC expire after 3 hours; all other posts expire after 12 hours
- All buttons survive bot restarts

## Setup

### 1. Create the bot application

1. Go to https://discord.com/developers/applications
2. Click "New Application", name it (e.g. "Let's Raid bot")
3. Go to the **Bot** tab, click "Reset Token", copy the token

### 2. Enable required intents

Still on the **Bot** tab, scroll to "Privileged Gateway Intents" and enable:
- **Server Members Intent**

### 3. Invite the bot to your server

1. Go to the **OAuth2** tab
2. Under "OAuth2 URL Generator", check: `bot` and `applications.commands`
3. Under "Bot Permissions", check:
   - Send Messages
   - Manage Messages
   - Embed Links
   - Read Message History
   - Move Members
   - Manage Roles
4. Copy the generated URL and open it in your browser, select your server

### 4. Server setup

Create these in your Discord server:

**Channels:**
- `#looking-for-game` -- text channel where LFG posts appear
- Voice channels -- all voice channels show up in the VC picker

**Roles:**
- `LFG PvP` -- pinged when someone creates a PvP game
- `LFG PvE` -- pinged when someone creates a PvE game

Make sure the bot's role (e.g. "Let's Raid bot") is **above** the LFG roles in the role list (Server Settings > Roles), otherwise it can't assign them.

**Custom emoji (optional):**
- Upload `assets/lfg_pvp.png` as a custom emoji named `lfg_pvp`
- Upload `assets/lfg_pve.png` as a custom emoji named `lfg_pve`

These can be used in role picker buttons.

### 5. Configure and run

Copy `.env.example` to `.env` and fill in your values:

```
BOT_TOKEN=your-bot-token
GUILD_IDS=your-server-id
```

Install and run:

```
uv sync
uv run python bot.py
```

Run tests:

```
uv run pytest tests/ -v
```

Or use the runner script:

```
./run.ps1
```

### 6. First-time bot commands

Run these once after the bot is online:

- `/lfgsetup` in your `#looking-for-game` channel -- posts the role picker and live game board. Pin the role picker for visibility.

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | Yes | -- | Discord bot token |
| `GUILD_IDS` | Yes | -- | Discord server ID(s), comma-separated |
| `LFG_CHANNEL_NAME` | No | `looking-for-game` | Text channel for LFG posts |
| `DB_PATH` | No | `letsraid.db` | SQLite database path |
| `LFG_PVP_ROLE` | No | `LFG PvP` | Role name for PvP pings |
| `LFG_PVE_ROLE` | No | `LFG PvE` | Role name for PvE pings |
| `AUTO_JOIN_VC` | No | `true` | Auto-move players to VC on join |
| `HIDDEN_VC` | No | -- | Voice channels to hide from VC picker (`guild:ch,ch;guild:ch`) |
| `HEALTHCHECK_URL` | No | -- | Healthchecks.io ping URL for uptime monitoring |

## Commands

| Command | Who | Description |
|---|---|---|
| `/lfg` | Everyone | Create an LFG post (pick PvP or PvE, fill in details) |
| `/lfglist` | Everyone | Show all active games |
| `/lfgstatus` | Everyone | Toggle your LFG roles with live-updating buttons |
| `/lfghelp` | Everyone | Show all LFG commands |
| `/lfgsetup` | Manage Roles + Channels | Post the role picker and live game board |

## License

MIT
