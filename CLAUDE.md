# LetsRaid - Discord LFG Bot

## Overview

A Discord bot for the First Wave Survivors server. Creates Looking For Game posts in `#looking-for-game` with interactive buttons for joining, leaving, and managing groups.

## Tech Stack

- Python 3.11+, discord.py 2.4+, aiosqlite, SQLite
- Slash commands via `app_commands`
- Persistent buttons via `DynamicItem`

## Running

```
pip install -r requirements.txt
python bot.py
```

Requires `.env` with `BOT_TOKEN` and `GUILD_ID`.

## Project Structure

- `bot.py` -- entry point, setup_hook, lifecycle
- `config.py` -- env var loading
- `db.py` -- database schema and query helpers
- `cogs/lfg.py` -- all LFG command/interaction logic
