import logging
import os

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import db

ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")

log = logging.getLogger("letsraid.lfg")

# -- Embed builder --------------------------------------------------------


def build_lfg_embed(
    post: dict, members: list[int], guild: discord.Guild
) -> discord.Embed:
    status = post["status"]
    color = {"open": discord.Color.green(), "full": discord.Color.gold(), "closed": discord.Color.red()}[status]

    mode_label = "PvP" if post["mode"] == "pvp" else "PvE"
    title = f"{mode_label}: {post['description']}" if post.get("description") else mode_label
    embed = discord.Embed(title=title[:256], color=color)

    # Mode icon as thumbnail (referenced via attachment://)
    icon_file = f"{post['mode']}.png"
    embed.set_thumbnail(url=f"attachment://{icon_file}")

    if post.get("start_time"):
        embed.add_field(name="Start Time / Duration", value=post["start_time"], inline=False)

    if post["voice_channel_id"]:
        embed.add_field(name="Voice Channel", value=f"<#{post['voice_channel_id']}>", inline=False)

    # Show pinged role (plain text so it works in DMs too)
    role_name = config.LFG_ROLE_NAMES.get(post["mode"])
    if role_name:
        embed.add_field(name="Looking For", value=role_name, inline=False)

    # Party list
    member_lines = []
    for i, uid in enumerate(members, 1):
        member = guild.get_member(uid)
        name = member.display_name if member else f"Unknown ({uid})"
        suffix = " (creator)" if uid == post["creator_id"] else ""
        member_lines.append(f"{i}. {name}{suffix}")

    slots_text = f"Party ({len(members)}/{post['max_slots']})"
    party_value = "\n".join(member_lines) if member_lines else "Empty"
    embed.add_field(name=slots_text, value=party_value, inline=False)

    if status == "closed":
        embed.add_field(name="Status", value="Closed", inline=False)

    creator = guild.get_member(post["creator_id"])
    footer = f"Created by {creator.display_name if creator else 'Unknown'}  |  LFG #{post['id']}"
    embed.set_footer(text=footer)
    # Use a dim color for footer by default (Discord handles footer text as grey)

    return embed


def get_mode_icon(mode: str) -> discord.File:
    path = os.path.join(ASSETS_DIR, f"{mode}.png")
    return discord.File(path, filename=f"{mode}.png")


# -- Persistent button view -----------------------------------------------


def build_lfg_view(lfg_id: int, status: str, owner_controls: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)

    is_closed = status == "closed"

    if not owner_controls:
        view.add_item(JoinButton(lfg_id, disabled=is_closed))
        view.add_item(JoinVCButton(lfg_id, disabled=is_closed))
        view.add_item(LeaveButton(lfg_id, disabled=is_closed))

    if owner_controls:
        view.add_item(GameFinishedButton(lfg_id, disabled=is_closed))

    return view


# -- DynamicItem buttons ---------------------------------------------------


class JoinButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lfg:join:(?P<id>\d+)"):
    def __init__(self, lfg_id: int, disabled: bool = False):
        super().__init__(
            discord.ui.Button(
                label="Join Game",
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
            return await interaction.response.send_message(f"LFG #{self.lfg_id}: This LFG no longer exists.", ephemeral=True)
        if post["status"] != "open":
            return await interaction.response.send_message(f"LFG #{self.lfg_id}: This LFG is not open.", ephemeral=True)

        members = await db.get_lfg_members(interaction.client.db, self.lfg_id)
        if interaction.user.id in members:
            return await interaction.response.send_message(f"LFG #{self.lfg_id}: You already joined.", ephemeral=True)
        if len(members) >= post["max_slots"]:
            return await interaction.response.send_message(f"LFG #{self.lfg_id}: Party is full.", ephemeral=True)

        added = await db.add_member(interaction.client.db, self.lfg_id, interaction.user.id)
        if not added:
            return await interaction.response.send_message(f"LFG #{self.lfg_id}: You already joined.", ephemeral=True)
        members.append(interaction.user.id)

        # Auto-full when slots filled
        status = post["status"]
        if len(members) >= post["max_slots"]:
            status = "full"
            await db.update_status(interaction.client.db, self.lfg_id, "full")

        post["status"] = status
        embed = build_lfg_embed(post, members, interaction.guild)
        view = build_lfg_view(self.lfg_id, status)
        await interaction.response.edit_message(embed=embed, view=view, attachments=[get_mode_icon(post["mode"])])

        # Move joiner to voice channel
        if post["voice_channel_id"] and interaction.guild:
            vc = interaction.guild.get_channel(post["voice_channel_id"])
            if vc:
                await try_move_to_vc(interaction.user, vc)

        await refresh_board(interaction.client)
        await update_vc_status(interaction.client, post, interaction.guild)


class JoinVCButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lfg:joinvc:(?P<id>\d+)"):
    def __init__(self, lfg_id: int, disabled: bool = False):
        super().__init__(
            discord.ui.Button(
                label="Join VC",
                style=discord.ButtonStyle.blurple,
                custom_id=f"lfg:joinvc:{lfg_id}",
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
            return await interaction.response.send_message(f"LFG #{self.lfg_id}: This LFG no longer exists.", ephemeral=True)
        if not post["voice_channel_id"]:
            return await interaction.response.send_message(f"LFG #{self.lfg_id}: No voice channel set.", ephemeral=True)

        vc = interaction.guild.get_channel(post["voice_channel_id"])
        if not vc:
            return await interaction.response.send_message(f"LFG #{self.lfg_id}: Voice channel not found.", ephemeral=True)

        if not interaction.user.voice:
            return await interaction.response.send_message(
                f"LFG #{self.lfg_id}: You need to be in a voice channel first. Join any VC, then click again.",
                ephemeral=True,
            )

        try:
            await interaction.user.move_to(vc)
            await interaction.response.send_message(f"LFG #{self.lfg_id}: Moved you to {vc.name}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(f"LFG #{self.lfg_id}: I don't have permission to move you.", ephemeral=True)


class LeaveButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lfg:leave:(?P<id>\d+)"):
    def __init__(self, lfg_id: int, disabled: bool = False):
        super().__init__(
            discord.ui.Button(
                label="Leave Party",
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
            return await interaction.response.send_message(f"LFG #{self.lfg_id}: This LFG no longer exists.", ephemeral=True)
        if interaction.user.id == post["creator_id"]:
            return await interaction.response.send_message(f"LFG #{self.lfg_id}: The creator can't leave. Use Game Finished or Delete instead.", ephemeral=True)

        removed = await db.remove_member(interaction.client.db, self.lfg_id, interaction.user.id)
        if not removed:
            return await interaction.response.send_message(f"LFG #{self.lfg_id}: You haven't joined this LFG.", ephemeral=True)

        members = await db.get_lfg_members(interaction.client.db, self.lfg_id)

        # Reopen if was full
        status = post["status"]
        if status == "full" and len(members) < post["max_slots"]:
            status = "open"
            await db.update_status(interaction.client.db, self.lfg_id, "open")

        post["status"] = status
        embed = build_lfg_embed(post, members, interaction.guild)
        view = build_lfg_view(self.lfg_id, status)
        await interaction.response.edit_message(embed=embed, view=view, attachments=[get_mode_icon(post["mode"])])

        await refresh_board(interaction.client)
        await update_vc_status(interaction.client, post, interaction.guild)


class GameFinishedButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lfg:finished:(?P<id>\d+)"):
    def __init__(self, lfg_id: int, disabled: bool = False):
        super().__init__(
            discord.ui.Button(
                label="Game Finished",
                style=discord.ButtonStyle.blurple,
                custom_id=f"lfg:finished:{lfg_id}",
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
            return await interaction.response.send_message(f"LFG #{self.lfg_id}: This LFG no longer exists.", ephemeral=True)
        if interaction.user.id != post["creator_id"]:
            return await interaction.response.send_message(f"LFG #{self.lfg_id}: Only the creator can finish this.", ephemeral=True)

        # Delete the channel post
        guild = interaction.client.get_guild(post["guild_id"])
        if guild:
            channel = guild.get_channel(post["channel_id"])
            if channel:
                try:
                    msg = await channel.fetch_message(post["message_id"])
                    await msg.delete()
                except discord.NotFound:
                    pass

        # Clear VC status before deleting
        post["status"] = "closed"
        if guild:
            await update_vc_status(interaction.client, post, guild)

        await db.delete_lfg(interaction.client.db, self.lfg_id)

        # Update the DM message
        await interaction.response.edit_message(
            content=f"LFG #{self.lfg_id}: Game finished. Post removed.",
            embed=None, view=None, attachments=[],
        )

        await refresh_board(interaction.client)


# -- Role toggle button ----------------------------------------------------


EMOJI_NAMES = {"pvp": "lfg_pvp", "pve": "lfg_pve"}


def get_lfg_emoji(guild: discord.Guild, mode: str) -> discord.Emoji | None:
    name = EMOJI_NAMES.get(mode)
    if name:
        return discord.utils.get(guild.emojis, name=name)
    return None


class RoleToggleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lfg:role:(?P<mode>pvp|pve)"):
    def __init__(self, mode: str, emoji: discord.Emoji | None = None):
        mode_label = "PvP" if mode == "pvp" else "PvE"
        super().__init__(
            discord.ui.Button(
                label=f"Looking to play {mode_label}",
                style=discord.ButtonStyle.green if mode == "pve" else discord.ButtonStyle.red,
                custom_id=f"lfg:role:{mode}",
                emoji=emoji,
            )
        )
        self.mode = mode

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["mode"])

    async def callback(self, interaction: discord.Interaction):
        role_name = config.LFG_ROLE_NAMES.get(self.mode)
        if not role_name:
            return await interaction.response.send_message("Role not configured.", ephemeral=True)

        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if not role:
            return await interaction.response.send_message(f"Role '{role_name}' not found on this server.", ephemeral=True)

        if role in interaction.user.roles:
            await interaction.user.remove_roles(role)
            await interaction.response.send_message(f"Removed **{role_name}** -- you won't be pinged for these games.", ephemeral=True)
        else:
            await interaction.user.add_roles(role)
            await interaction.response.send_message(f"Added **{role_name}** -- you'll be pinged when someone creates a game!", ephemeral=True)


class LFGNowView(discord.ui.View):
    def __init__(self, member: discord.Member):
        super().__init__(timeout=120)
        self.member = member
        user_roles = [r.name for r in member.roles]

        for mode in ("pvp", "pve"):
            role_name = config.LFG_ROLE_NAMES.get(mode, "")
            mode_label = "PvP" if mode == "pvp" else "PvE"
            has_role = role_name in user_roles

            emoji = get_lfg_emoji(member.guild, mode)
            button = discord.ui.Button(
                label=f"Stop looking for {mode_label}" if has_role else f"Looking for {mode_label}",
                style=discord.ButtonStyle.red if has_role else discord.ButtonStyle.green,
                custom_id=f"lfgstatus:{mode}",
                emoji=emoji,
            )
            button.callback = self._make_callback(mode, role_name)
            self.add_item(button)

    def _make_callback(self, mode: str, role_name: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.member.id:
                return await interaction.response.send_message("This isn't for you.", ephemeral=True)

            role = discord.utils.get(interaction.guild.roles, name=role_name)
            if not role:
                return await interaction.response.send_message(f"Role '{role_name}' not found.", ephemeral=True)

            if role in interaction.user.roles:
                await interaction.user.remove_roles(role)
            else:
                await interaction.user.add_roles(role)

            # Refetch member to get updated roles
            member = await interaction.guild.fetch_member(interaction.user.id)
            new_view = LFGNowView(member)
            await interaction.response.edit_message(view=new_view)

        return callback


class LFGStartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="PvP", style=discord.ButtonStyle.red, custom_id="lfgstart:pvp")
    async def pvp_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LFGModal("pvp"))

    @discord.ui.button(label="PvE", style=discord.ButtonStyle.green, custom_id="lfgstart:pve")
    async def pve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LFGModal("pve"))


# -- Modal -----------------------------------------------------------------


class LFGModal(discord.ui.Modal, title="Create LFG Post"):
    description_input = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.short,
        placeholder="Map name, playstyle, notes...",
        max_length=200,
        required=False,
    )
    start_time_input = discord.ui.TextInput(
        label="Start Time / Duration",
        style=discord.TextStyle.short,
        placeholder="e.g. now, 8pm EST, for 2 hours",
        max_length=100,
        required=False,
    )
    max_slots_input = discord.ui.TextInput(
        label="Party Size (2 or 3)",
        style=discord.TextStyle.short,
        placeholder="3",
        default="3",
        max_length=1,
        required=True,
    )

    def __init__(self, mode_value: str):
        super().__init__()
        self.mode_value = mode_value

        self.vc_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.voice, discord.ChannelType.stage_voice],
            placeholder="Select a voice channel",
            min_values=0,
            max_values=1,
        )
        self.add_item(discord.ui.Label(text="Voice Channel", component=self.vc_select))

    async def on_submit(self, interaction: discord.Interaction):
        # Validate max_slots
        try:
            max_slots = int(self.max_slots_input.value)
            if max_slots not in (2, 3):
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                "Party size must be 2 or 3.", ephemeral=True
            )

        # Find LFG channel
        lfg_channel = discord.utils.get(
            interaction.guild.text_channels, name=config.LFG_CHANNEL_NAME
        )
        if not lfg_channel:
            return await interaction.response.send_message(
                f"Could not find #{config.LFG_CHANNEL_NAME} channel.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        # Resolve voice channel: user's selection > current VC > least-full VC* channel
        voice_channel = None
        vc_warning = None

        if self.vc_select.values:
            vc_ref = self.vc_select.values[0]
            voice_channel = interaction.guild.get_channel(vc_ref.id)
        elif interaction.user.voice and interaction.user.voice.channel:
            voice_channel = interaction.user.voice.channel
        else:
            voice_channel = find_least_full_voice_channel(interaction.guild)
            if voice_channel and len(voice_channel.members) > 0:
                vc_warning = f"Note: {voice_channel.name} has {len(voice_channel.members)} participant(s) already."

        # Find the role to ping
        role_name = config.LFG_ROLE_NAMES.get(self.mode_value)
        role = discord.utils.get(interaction.guild.roles, name=role_name) if role_name else None

        description = self.description_input.value or None
        start_time = self.start_time_input.value or None

        # Insert into DB first (with placeholder message_id) to get the real LFG ID
        lfg_id = await db.create_lfg(
            interaction.client.db,
            message_id=0,
            channel_id=lfg_channel.id,
            guild_id=interaction.guild.id,
            creator_id=interaction.user.id,
            voice_channel_id=voice_channel.id if voice_channel else None,
            mode=self.mode_value,
            description=description,
            start_time=start_time,
            max_slots=max_slots,
        )

        # Build the final embed and view with the real LFG ID
        post = {
            "id": lfg_id,
            "creator_id": interaction.user.id,
            "voice_channel_id": voice_channel.id if voice_channel else None,
            "mode": self.mode_value,
            "description": description,
            "start_time": start_time,
            "max_slots": max_slots,
            "status": "open",
        }
        members = [interaction.user.id]
        embed = build_lfg_embed(post, members, interaction.guild)
        channel_view = build_lfg_view(lfg_id, "open")

        # Send once with everything -- no edit needed
        ping_content = role.mention if role else None
        msg = await lfg_channel.send(
            content=ping_content, embed=embed, view=channel_view,
            file=get_mode_icon(self.mode_value),
        )

        # Update DB with the real message ID
        await db.update_message_id(interaction.client.db, lfg_id, msg.id)

        # Move creator to voice channel
        if voice_channel:
            creator_member = interaction.guild.get_member(interaction.user.id)
            if creator_member:
                await try_move_to_vc(creator_member, voice_channel)

        # DM creator with full controls (Game Finished, Delete)
        try:
            dm_embed = build_lfg_embed(post, members, interaction.guild)
            dm_embed.add_field(name="Channel Post", value=f"[Jump to post]({msg.jump_url})", inline=False)
            owner_view = build_lfg_view(lfg_id, "open", owner_controls=True)
            await interaction.user.send(
                content="Hit **Game Finished** when you're done to clean up your post!",
                embed=dm_embed, view=owner_view, file=get_mode_icon(self.mode_value),
            )
        except discord.Forbidden:
            log.warning("Could not DM creator %s for LFG #%s", interaction.user.id, lfg_id)

        await refresh_board(interaction.client)
        await update_vc_status(interaction.client, post, interaction.guild)

        followup_msg = f"LFG #{lfg_id}: Post created in {lfg_channel.mention}!"
        if vc_warning:
            followup_msg += f"\n{vc_warning}"
        await interaction.followup.send(followup_msg, ephemeral=True)


# -- Helpers ---------------------------------------------------------------


def get_vc_channels(guild: discord.Guild) -> list[discord.VoiceChannel]:
    return [ch for ch in guild.voice_channels if ch.name.startswith(config.VC_PREFIX)]


def find_least_full_voice_channel(guild: discord.Guild) -> discord.VoiceChannel | None:
    channels = get_vc_channels(guild)
    if not channels:
        return None
    return min(channels, key=lambda ch: len(ch.members))


async def try_move_to_vc(member: discord.Member, channel: discord.VoiceChannel):
    if not config.AUTO_JOIN_VC:
        return
    if member.voice:
        try:
            await member.move_to(channel)
        except discord.Forbidden:
            log.warning("No permission to move %s to %s", member, channel)


# -- Board -----------------------------------------------------------------


async def build_board_embed(bot_db: aiosqlite.Connection, guild: discord.Guild) -> discord.Embed:
    posts = await db.get_open_posts(bot_db, guild.id)

    embed = discord.Embed(
        title="Active Games",
        color=discord.Color.blurple(),
    )

    if not posts:
        embed.description = "No active games right now. Use `/lfg` to start one!"
        return embed

    for post in posts:
        mode_label = "PvP" if post["mode"] == "pvp" else "PvE"
        members = await db.get_lfg_members(bot_db, post["id"])
        title = f"#{post['id']} - {mode_label}"
        if post["description"]:
            title += f": {post['description'][:50]}"

        lines = [f"Party: {len(members)}/{post['max_slots']}"]
        if post["start_time"]:
            lines.append(f"Start: {post['start_time']}")
        if post["voice_channel_id"]:
            lines.append(f"VC: <#{post['voice_channel_id']}>")

        creator = guild.get_member(post["creator_id"])
        creator_name = creator.display_name if creator else "Unknown"
        lines.append(f"Host: {creator_name}")

        if post["status"] == "full":
            lines.append("**FULL**")

        embed.add_field(name=title, value="\n".join(lines), inline=False)

    embed.set_footer(text=f"{len(posts)} active game{'s' if len(posts) != 1 else ''}")
    return embed


async def refresh_board(bot):
    for guild in bot.guilds:
        board = await db.get_board(bot.db, guild.id)
        if not board:
            continue
        channel = guild.get_channel(board["channel_id"])
        if not channel:
            continue
        try:
            msg = await channel.fetch_message(board["message_id"])
            embed = await build_board_embed(bot.db, guild)
            await msg.edit(embed=embed)
        except discord.NotFound:
            log.warning("Board message not found for guild %s", guild.id)
        except Exception:
            log.exception("Error refreshing board for guild %s", guild.id)


async def update_vc_status(bot, post: dict | None, guild: discord.Guild):
    """Set or clear the voice channel status for an LFG post's VC."""
    if not post or not post.get("voice_channel_id"):
        return
    vc = guild.get_channel(post["voice_channel_id"])
    if not vc:
        return

    try:
        if post["status"] in ("open", "full"):
            mode_label = "PvP" if post["mode"] == "pvp" else "PvE"
            members = await db.get_lfg_members(bot.db, post["id"])
            status_parts = [f"{mode_label} ({len(members)}/{post['max_slots']})"]
            if post.get("description"):
                status_parts.append(post["description"][:80])
            await vc.edit(status=" - ".join(status_parts))
        else:
            await vc.edit(status=None)
    except discord.Forbidden:
        log.warning("No permission to set VC status on %s", vc.name)
    except Exception:
        log.exception("Error updating VC status for %s", vc.name)


# -- Cog -------------------------------------------------------------------


class LFGCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_dynamic_items(
            JoinButton, JoinVCButton, LeaveButton, GameFinishedButton, RoleToggleButton
        )
        self.cleanup_old_posts.start()
        log.info("LFG cog loaded")

    async def cog_unload(self):
        self.bot.remove_dynamic_items(
            JoinButton, JoinVCButton, LeaveButton, GameFinishedButton, RoleToggleButton
        )
        self.cleanup_old_posts.cancel()

    @app_commands.command(name="lfg", description="Create a Looking For Game post")
    @app_commands.describe(mode="PvP or PvE")
    @app_commands.choices(mode=[
        app_commands.Choice(name="pvp", value="pvp"),
        app_commands.Choice(name="pve", value="pve"),
    ])
    async def lfg(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        await interaction.response.send_modal(LFGModal(mode.value))

    @app_commands.command(name="lfgsetup", description="Post the role picker and live game board in this channel")
    @app_commands.checks.has_permissions(manage_roles=True, manage_channels=True)
    async def lfgsetup(self, interaction: discord.Interaction):
        await interaction.response.send_message("Setting up LFG channel...", ephemeral=True)

        # Post role picker
        role_embed = discord.Embed(
            title="I'm looking for a game!",
            description="Choose the type of game you want to be notified for. You can choose one, both, or neither to disable pings.",
            color=discord.Color.blurple(),
        )
        role_view = discord.ui.View(timeout=None)
        role_view.add_item(RoleToggleButton("pvp", get_lfg_emoji(interaction.guild, "pvp")))
        role_view.add_item(RoleToggleButton("pve", get_lfg_emoji(interaction.guild, "pve")))
        await interaction.channel.send(embed=role_embed, view=role_view)

        # Post live board
        board_embed = await build_board_embed(interaction.client.db, interaction.guild)
        board_msg = await interaction.channel.send(embed=board_embed)
        await db.set_board(interaction.client.db, interaction.guild.id, interaction.channel.id, board_msg.id)

        await interaction.followup.send("Done! Role picker and game board posted.", ephemeral=True)

    @app_commands.command(name="lfgstatus", description="Toggle your LFG roles")
    async def lfgstatus(self, interaction: discord.Interaction):
        view = LFGNowView(interaction.user)
        await interaction.response.send_message(
            "Looking for game settings:", view=view, ephemeral=True
        )

    @app_commands.command(name="lfglist", description="Show all active games")
    async def lfglist(self, interaction: discord.Interaction):
        embed = await build_board_embed(interaction.client.db, interaction.guild)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="lfghelp", description="Show all LFG commands")
    async def lfghelp(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Let's Raid bot",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="/lfg",
            value="Create a Looking For Game post (PvP or PvE)",
            inline=False,
        )
        embed.add_field(
            name="/lfglist",
            value="See all active games",
            inline=False,
        )
        embed.add_field(
            name="/lfgstatus",
            value="Toggle your LFG notification roles",
            inline=False,
        )
        embed.add_field(
            name="Right-click menu",
            value="Right-click any user and look under **Apps** for:\n"
                  "- **Start looking for game** -- create an LFG post\n"
                  "- **Looking for Game settings** -- toggle your notification roles",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tasks.loop(minutes=5)
    async def cleanup_old_posts(self):
        expired = await db.get_expired_posts(self.bot.db, hours=3)
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
                post_dict["description"] = f"[Expired] {post['description'] or ''}"
                embed = build_lfg_embed(post_dict, members, guild)
                view = build_lfg_view(post["id"], "closed")
                await msg.edit(embed=embed, view=view, attachments=[get_mode_icon(post_dict["mode"])])
                await update_vc_status(self.bot, post_dict, guild)
            except discord.NotFound:
                await db.delete_lfg(self.bot.db, post["id"])
            except Exception:
                log.exception("Error expiring LFG post %s", post["id"])
        if expired:
            await refresh_board(self.bot)

        # Purge closed posts older than 24h from the database
        await db.delete_old_closed_posts(self.bot.db, hours=24)

    @cleanup_old_posts.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()


# -- Context menu commands (must be at module level) -----------------------


@app_commands.context_menu(name="Start looking for game")
async def ctx_start_lfg(interaction: discord.Interaction, user: discord.User):
    view = LFGStartView()
    await interaction.response.send_message(
        "Pick a mode to start:", view=view, ephemeral=True
    )


@app_commands.context_menu(name="Looking for Game settings")
async def ctx_lfg_settings(interaction: discord.Interaction, user: discord.User):
    view = LFGNowView(interaction.user)
    await interaction.response.send_message(
        "Looking for game settings:", view=view, ephemeral=True
    )


async def setup(bot: commands.Bot):
    await bot.add_cog(LFGCog(bot))
    bot.tree.add_command(ctx_start_lfg)
    bot.tree.add_command(ctx_lfg_settings)


async def teardown(bot: commands.Bot):
    bot.tree.remove_command(ctx_start_lfg.name, type=ctx_start_lfg.type)
    bot.tree.remove_command(ctx_lfg_settings.name, type=ctx_lfg_settings.type)
