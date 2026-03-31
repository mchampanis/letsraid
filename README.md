# LetsRaid

Discord LFG (Looking For Game) bot for the First Wave Survivors server.

## Features

- `/lfg` slash command to create LFG posts in `#looking-for-game`
- Interactive buttons: Join, Leave, Close, Full/Open toggle, Delete
- Role pings to notify interested players
- Voice channel links for quick joining
- Posts auto-expire after 24 hours
- Buttons survive bot restarts

## Setup

1. Create a Discord application at https://discord.com/developers/applications
2. Enable the **Server Members Intent** under Bot settings
3. Invite the bot with these permissions: Send Messages, Embed Links, Mention Everyone, Read Message History, View Channels
4. Copy `.env.example` to `.env` and fill in your token and guild ID

```
pip install -r requirements.txt
python bot.py
```

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | Yes | -- | Discord bot token |
| `GUILD_ID` | Yes | -- | Discord server ID |
| `LFG_CHANNEL_NAME` | No | `looking-for-game` | Channel name for LFG posts |
| `DB_PATH` | No | `letsraid.db` | SQLite database path |

## Usage

1. Type `/lfg max_slots:6` in any channel
2. Fill in the description and start time in the modal
3. Pick a voice channel and roles to ping
4. Click "Create LFG Post"

The post appears in `#looking-for-game` with interactive buttons for other players to join.
