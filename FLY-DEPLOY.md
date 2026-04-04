# Fly.io Deployment

## Quick Setup

### 1. Create app and volume

```bash
fly launch --no-deploy
fly volumes create letsraid_data --region ams --size 1
```

### 2. Set secrets

These replace your local `.env` file. Fly injects them as environment variables.

```bash
fly secrets set BOT_TOKEN="your-token"
fly secrets set GUILD_IDS="id1,id2"
```

### 3. Deploy

```bash
fly deploy
```

## Configuration

Non-secret config lives in `fly.toml` under `[env]`. Secrets are set via `fly secrets set`.

| Variable | Where | Description |
|---|---|---|
| `BOT_TOKEN` | `fly secrets` | Discord bot token |
| `GUILD_IDS` | `fly secrets` | Discord server ID(s), comma-separated |
| `DB_PATH` | `fly.toml [env]` | SQLite path (must be on mounted volume) |
| `HIDDEN_VC` | `fly.toml [env]` | VC channels to hide from picker |

## Key Details

- **No `[http_service]`** -- this is a worker process, not a web server. Fly won't run HTTP health checks or auto-stop it.
- **Volume** is pinned to `ams` region. The app's `primary_region` must match.
- **Single instance only** -- SQLite doesn't support concurrent writers across machines, and only one bot process can hold the Discord gateway connection.
- **Auto-restart** -- if the bot crashes, Fly restarts it automatically.
- **`load_dotenv()`** is a no-op when no `.env` file exists. Fly secrets are already env vars.

## Monitoring

Set `HEALTHCHECK_URL` to a [Healthchecks.io](https://healthchecks.io) ping URL to get notified if the bot goes down. The bot pings every 5 minutes. Recommended HC schedule: 10-minute period, 10-minute grace.

```bash
fly secrets set HEALTHCHECK_URL="https://hc-ping.com/your-uuid"
```

## Logs

```bash
fly logs                # Stream live logs (Ctrl+C to stop)
fly logs --app letsraid # Explicit app name (if you have multiple apps)
```

Logs are also available in the Fly dashboard at https://fly.io/apps/letsraid/monitoring.

## Useful Commands

```bash
fly status              # Check app status
fly deploy              # Deploy latest changes
fly ssh console         # SSH into the machine
fly secrets list        # List set secrets
fly secrets set K=V     # Set a secret
fly secrets unset X     # Remove a secret
fly volumes list        # List volumes
```
