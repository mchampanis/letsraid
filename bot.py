import logging

import aiohttp
import aiosqlite
import discord
from discord.ext import commands, tasks

import config
import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("letsraid")

intents = discord.Intents.default()
intents.members = True


class LetsRaidBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Database
        self.db = await aiosqlite.connect(config.DB_PATH)
        self.db.row_factory = aiosqlite.Row
        await db.init_db(self.db)
        log.info("Database initialized")

        # Load cog (registers dynamic items internally)
        await self.load_extension("cogs.lfg")

        # Sync slash commands to each configured guild (instant)
        for guild_id in config.GUILD_IDS:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
            except discord.Forbidden:
                # Bot isn't in this guild, or was invited without the
                # applications.commands scope. Skip so one bad ID doesn't
                # take the whole process down.
                log.warning(
                    "Cannot sync commands to guild %s: missing access "
                    "(check GUILD_IDS and that the bot was invited with "
                    "the applications.commands scope)",
                    guild_id,
                )
                continue
            log.info("Commands synced to guild %s", guild_id)

    async def close(self):
        if hasattr(self, "db"):
            await self.db.close()
            log.info("Database connection closed")
        await super().close()

    async def on_ready(self):
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        if config.HEALTHCHECK_URL and not self.heartbeat.is_running():
            self.heartbeat.start()

    @tasks.loop(minutes=5)
    async def heartbeat(self):
        try:
            async with aiohttp.ClientSession() as session:
                await session.get(config.HEALTHCHECK_URL)
        except Exception:
            log.warning("Healthcheck ping failed", exc_info=True)


bot = LetsRaidBot()
bot.run(config.BOT_TOKEN, log_handler=None)
