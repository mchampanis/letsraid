# LetsRaid bot launcher
param(
    [Parameter(Position=0)]
    [ValidateSet("bot", "deploy", "deploy:check")]
    [string]$Command = "bot"
)

switch ($Command) {
    "bot" {
        uv run python bot.py
    }
    "deploy" {
        git rev-parse --short HEAD | Out-File -NoNewline -Encoding ascii COMMIT
        fly deploy
    }
    "deploy:check" {
        fly ssh console -C "sh -c 'cat /app/COMMIT 2>/dev/null || echo unknown'"
    }
}
