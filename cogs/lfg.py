import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import db

log = logging.getLogger("letsraid.lfg")

# -- Embed builder --------------------------------------------------------


def build_lfg_embed(
    post: dict, members: list[int], guild: discord.Guild
) -> discord.Embed:
    status = post["status"]
    color = {"open": discord.Color.green(), "full": discord.Color.gold(), "closed": discord.Color.red()}[status]

    embed = discord.Embed(
        title=post["description"][:256],
        color=color,
    )

    embed.add_field(name="Start Time", value=post["start_time"], inline=True)

    if post["voice_channel_id"]:
        embed.add_field(name="Voice Channel", value=f"<#{post['voice_channel_id']}>", inline=True)

    # Party list
    member_lines = []
    for i, uid in enumerate(members, 1):
        member = guild.get_member(uid)
        name = member.display_name if member else f"Unknown ({uid})"
        prefix = "**>** " if uid == post["creator_id"] else ""
        member_lines.append(f"{i}. {prefix}{name}")

    slots_text = f"Party ({len(members)}/{post['max_slots']})"
    party_value = "\n".join(member_lines) if member_lines else "Empty"
    embed.add_field(name=slots_text, value=party_value, inline=False)

    if status == "closed":
        embed.add_field(name="Status", value="Closed", inline=True)
    elif status == "full":
        embed.add_field(name="Status", value="Full", inline=True)

    creator = guild.get_member(post["creator_id"])
    footer = f"Created by {creator.display_name if creator else 'Unknown'}"
    embed.set_footer(text=f"{footer}  |  LFG #{post['id']}")

    return embed


# -- Persistent button view -----------------------------------------------


def build_lfg_view(lfg_id: int, status: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)

    is_full = status == "full"
    is_closed = status == "closed"

    view.add_item(JoinButton(lfg_id, disabled=is_full or is_closed))
    view.add_item(LeaveButton(lfg_id, disabled=is_closed))
    view.add_item(ToggleButton(lfg_id, currently_full=is_full, disabled=is_closed))
    view.add_item(CloseButton(lfg_id, disabled=is_closed))
    view.add_item(DeleteButton(lfg_id))

    return view


# -- DynamicItem buttons ---------------------------------------------------


class JoinButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lfg:join:(?P<id>\d+)"):
    def __init__(self, lfg_id: int, disabled: bool = False):
        super().__init__(
            discord.ui.Button(
                label="Join",
                style=discord.ButtonStyle.green,
                custom_id=f"lfg:join:{lfg_id}",
                disabled=disabled,
            )
        )
        self.lfg_id = lfg_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        post = await db.get_lfg(interaction.client.db, self.lfg_id)
        if not post:
            return await interaction.response.send_message("This LFG no longer exists.", ephemeral=True)
        if post["status"] != "open":
            return await interaction.response.send_message("This LFG is not open.", ephemeral=True)

        members = await db.get_lfg_members(interaction.client.db, self.lfg_id)
        if interaction.user.id in members:
            return await interaction.response.send_message("You already joined.", ephemeral=True)
        if len(members) >= post["max_slots"]:
            return await interaction.response.send_message("Party is full.", ephemeral=True)

        await db.add_member(interaction.client.db, self.lfg_id, interaction.user.id)
        members.append(interaction.user.id)

        # Auto-full when slots filled
        status = post["status"]
        if len(members) >= post["max_slots"]:
            status = "full"
            await db.update_status(interaction.client.db, self.lfg_id, "full")

        post["status"] = status
        embed = build_lfg_embed(post, members, interaction.guild)
        view = build_lfg_view(self.lfg_id, status)
        await interaction.response.edit_message(embed=embed, view=view)


class LeaveButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lfg:leave:(?P<id>\d+)"):
    def __init__(self, lfg_id: int, disabled: bool = False):
        super().__init__(
            discord.ui.Button(
                label="Leave",
                style=discord.ButtonStyle.grey,
                custom_id=f"lfg:leave:{lfg_id}",
                disabled=disabled,
            )
        )
        self.lfg_id = lfg_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        post = await db.get_lfg(interaction.client.db, self.lfg_id)
        if not post:
            return await interaction.response.send_message("This LFG no longer exists.", ephemeral=True)
        if interaction.user.id == post["creator_id"]:
            return await interaction.response.send_message("The creator can't leave. Use Close or Delete instead.", ephemeral=True)

        removed = await db.remove_member(interaction.client.db, self.lfg_id, interaction.user.id)
        if not removed:
            return await interaction.response.send_message("You haven't joined this LFG.", ephemeral=True)

        members = await db.get_lfg_members(interaction.client.db, self.lfg_id)

        # Reopen if was full
        status = post["status"]
        if status == "full" and len(members) < post["max_slots"]:
            status = "open"
            await db.update_status(interaction.client.db, self.lfg_id, "open")

        post["status"] = status
        embed = build_lfg_embed(post, members, interaction.guild)
        view = build_lfg_view(self.lfg_id, status)
        await interaction.response.edit_message(embed=embed, view=view)


class CloseButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lfg:close:(?P<id>\d+)"):
    def __init__(self, lfg_id: int, disabled: bool = False):
        super().__init__(
            discord.ui.Button(
                label="Close",
                style=discord.ButtonStyle.red,
                custom_id=f"lfg:close:{lfg_id}",
                disabled=disabled,
            )
        )
        self.lfg_id = lfg_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        post = await db.get_lfg(interaction.client.db, self.lfg_id)
        if not post:
            return await interaction.response.send_message("This LFG no longer exists.", ephemeral=True)
        if interaction.user.id != post["creator_id"]:
            return await interaction.response.send_message("Only the creator can close this.", ephemeral=True)

        await db.update_status(interaction.client.db, self.lfg_id, "closed")
        post["status"] = "closed"
        members = await db.get_lfg_members(interaction.client.db, self.lfg_id)
        embed = build_lfg_embed(post, members, interaction.guild)
        view = build_lfg_view(self.lfg_id, "closed")
        await interaction.response.edit_message(embed=embed, view=view)


class ToggleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lfg:toggle:(?P<id>\d+)"):
    def __init__(self, lfg_id: int, currently_full: bool = False, disabled: bool = False):
        label = "Open" if currently_full else "Full"
        super().__init__(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.blurple,
                custom_id=f"lfg:toggle:{lfg_id}",
                disabled=disabled,
            )
        )
        self.lfg_id = lfg_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        post = await db.get_lfg(interaction.client.db, self.lfg_id)
        if not post:
            return await interaction.response.send_message("This LFG no longer exists.", ephemeral=True)
        if interaction.user.id != post["creator_id"]:
            return await interaction.response.send_message("Only the creator can toggle this.", ephemeral=True)
        if post["status"] == "closed":
            return await interaction.response.send_message("This LFG is closed.", ephemeral=True)

        new_status = "open" if post["status"] == "full" else "full"
        await db.update_status(interaction.client.db, self.lfg_id, new_status)
        post["status"] = new_status
        members = await db.get_lfg_members(interaction.client.db, self.lfg_id)
        embed = build_lfg_embed(post, members, interaction.guild)
        view = build_lfg_view(self.lfg_id, new_status)
        await interaction.response.edit_message(embed=embed, view=view)


class DeleteButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lfg:delete:(?P<id>\d+)"):
    def __init__(self, lfg_id: int):
        super().__init__(
            discord.ui.Button(
                label="Delete",
                style=discord.ButtonStyle.red,
                custom_id=f"lfg:delete:{lfg_id}",
                emoji="\U0001f5d1",
            )
        )
        self.lfg_id = lfg_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        post = await db.get_lfg(interaction.client.db, self.lfg_id)
        if not post:
            return await interaction.response.send_message("This LFG no longer exists.", ephemeral=True)
        if interaction.user.id != post["creator_id"]:
            return await interaction.response.send_message("Only the creator can delete this.", ephemeral=True)

        await db.delete_lfg(interaction.client.db, self.lfg_id)
        await interaction.response.send_message("LFG post deleted.", ephemeral=True)
        await interaction.message.delete()


# -- Modal and setup view --------------------------------------------------


class LFGModal(discord.ui.Modal, title="Create LFG Post"):
    description_input = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.long,
        placeholder="What are you playing? Any requirements?",
        max_length=1000,
        required=True,
    )
    start_time_input = discord.ui.TextInput(
        label="Start Time",
        style=discord.TextStyle.short,
        placeholder="e.g. 8pm EST, in 2 hours, Friday 9pm",
        max_length=100,
        required=True,
    )

    def __init__(self, max_slots: int):
        super().__init__()
        self.max_slots = max_slots

    async def on_submit(self, interaction: discord.Interaction):
        view = LFGSetupView(
            description=self.description_input.value,
            start_time=self.start_time_input.value,
            max_slots=self.max_slots,
            creator_id=interaction.user.id,
        )
        await interaction.response.send_message(
            "**Pick a voice channel and roles to ping, then click Create.**",
            view=view,
            ephemeral=True,
        )


class LFGSetupView(discord.ui.View):
    def __init__(self, *, description: str, start_time: str, max_slots: int, creator_id: int):
        super().__init__(timeout=180)
        self.description = description
        self.start_time = start_time
        self.max_slots = max_slots
        self.creator_id = creator_id
        self.selected_channel: discord.app_commands.AppCommandChannel | None = None
        self.selected_roles: list[discord.Role] = []

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.voice, discord.ChannelType.stage_voice],
        placeholder="Select a voice channel",
        min_values=1,
        max_values=1,
    )
    async def channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.selected_channel = select.values[0]
        await interaction.response.defer()

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Select roles to ping (optional)",
        min_values=0,
        max_values=10,
    )
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.selected_roles = select.values
        await interaction.response.defer()

    @discord.ui.button(label="Create LFG Post", style=discord.ButtonStyle.green, row=3)
    async def create_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_channel:
            return await interaction.response.send_message(
                "Please select a voice channel first.", ephemeral=True
            )

        # Find the LFG channel
        lfg_channel = discord.utils.get(
            interaction.guild.text_channels, name=config.LFG_CHANNEL_NAME
        )
        if not lfg_channel:
            return await interaction.response.send_message(
                f"Could not find #{config.LFG_CHANNEL_NAME} channel.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        # Resolve the voice channel to a full object
        voice_channel = interaction.guild.get_channel(self.selected_channel.id)

        # Build a temporary post dict for embed (id will be set after DB insert)
        temp_post = {
            "id": 0,
            "creator_id": self.creator_id,
            "voice_channel_id": voice_channel.id if voice_channel else None,
            "description": self.description,
            "start_time": self.start_time,
            "max_slots": self.max_slots,
            "status": "open",
        }
        members = [self.creator_id]
        embed = build_lfg_embed(temp_post, members, interaction.guild)

        # Send embed (without buttons yet, we need the DB id first)
        msg = await lfg_channel.send(embed=embed)

        # Insert into DB
        lfg_id = await db.create_lfg(
            interaction.client.db,
            message_id=msg.id,
            channel_id=lfg_channel.id,
            guild_id=interaction.guild.id,
            creator_id=self.creator_id,
            voice_channel_id=voice_channel.id if voice_channel else None,
            description=self.description,
            start_time=self.start_time,
            max_slots=self.max_slots,
            role_ids=[r.id for r in self.selected_roles],
        )

        # Rebuild embed with real id and attach buttons
        temp_post["id"] = lfg_id
        embed = build_lfg_embed(temp_post, members, interaction.guild)
        view = build_lfg_view(lfg_id, "open")
        await msg.edit(embed=embed, view=view)

        # Ping roles in a separate message so the notification works
        if self.selected_roles:
            pings = " ".join(r.mention for r in self.selected_roles)
            await lfg_channel.send(f"{pings} -- New LFG post!")

        await interaction.followup.send(
            f"LFG post created in {lfg_channel.mention}!", ephemeral=True
        )
        self.stop()


# -- Cog -------------------------------------------------------------------


class LFGCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_dynamic_items(
            JoinButton, LeaveButton, CloseButton, ToggleButton, DeleteButton
        )
        self.cleanup_old_posts.start()
        log.info("LFG cog loaded")

    async def cog_unload(self):
        self.bot.remove_dynamic_items(
            JoinButton, LeaveButton, CloseButton, ToggleButton, DeleteButton
        )
        self.cleanup_old_posts.cancel()

    @app_commands.command(name="lfg", description="Create a Looking For Game post")
    @app_commands.describe(max_slots="Number of player slots including you (2-24)")
    async def lfg(self, interaction: discord.Interaction, max_slots: app_commands.Range[int, 2, 24]):
        await interaction.response.send_modal(LFGModal(max_slots))

    @tasks.loop(minutes=5)
    async def cleanup_old_posts(self):
        expired = await db.get_expired_posts(self.bot.db, hours=24)
        for post in expired:
            try:
                await db.update_status(self.bot.db, post["id"], "closed")
                guild = self.bot.get_guild(post["guild_id"])
                if not guild:
                    continue
                channel = guild.get_channel(post["channel_id"])
                if not channel:
                    continue
                msg = await channel.fetch_message(post["message_id"])
                members = await db.get_lfg_members(self.bot.db, post["id"])
                post_dict = dict(post)
                post_dict["status"] = "closed"
                post_dict["description"] = f"[Expired] {post['description']}"
                embed = build_lfg_embed(post_dict, members, guild)
                view = build_lfg_view(post["id"], "closed")
                await msg.edit(embed=embed, view=view)
            except discord.NotFound:
                await db.delete_lfg(self.bot.db, post["id"])
            except Exception:
                log.exception("Error expiring LFG post %s", post["id"])

    @cleanup_old_posts.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(LFGCog(bot))
