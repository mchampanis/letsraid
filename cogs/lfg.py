import logging
import os
import time
from datetime import datetime, timezone

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
    icon_file = f"lfg_{post['mode']}.png"
    embed.set_thumbnail(url=f"attachment://{icon_file}")

    if post.get("start_time"):
        start_value = post["start_time"]
        if post.get("created_at"):
            dt = datetime.strptime(post["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            ts = int(dt.timestamp())
            start_value += f" (posted <t:{ts}:R>)"
        embed.add_field(name="Start Time / Duration", value=start_value, inline=False)

    if post["voice_channel_id"]:
        embed.add_field(name="Voice Channel", value=f"<#{post['voice_channel_id']}>", inline=False)

    # Show pinged role (plain text so it works in DMs too)
    role_name = config.LFG_ROLE_NAMES.get(post["mode"])
    if role_name:
        embed.add_field(name="Looking For", value=mode_label, inline=False)

    # Party list -- show filled slots, then empty placeholders up to max_slots
    member_lines = []
    for i, uid in enumerate(members, 1):
        member = guild.get_member(uid)
        name = member.display_name if member else f"Unknown ({uid})"
        suffix = " (creator)" if uid == post["creator_id"] else ""
        member_lines.append(f"{i}. {name}{suffix}")
    for i in range(len(members) + 1, post["max_slots"] + 1):
        member_lines.append(f"{i}. __\u2002\u2002\u2002__ *(open)*")

    slots_text = f"Players ({len(members)}/{post['max_slots']})"
    embed.add_field(name=slots_text, value="\n".join(member_lines), inline=False)

    if status == "full":
        embed.add_field(name="Status", value="Game full", inline=False)
    elif status == "closed":
        embed.add_field(name="Status", value="Finished", inline=False)

    creator = guild.get_member(post["creator_id"])
    seq = post.get("guild_seq", post["id"])
    footer = f"Created by {creator.display_name if creator else 'Unknown'}  |  LFG #{seq}"
    embed.set_footer(text=footer)
    # Use a dim color for footer by default (Discord handles footer text as grey)

    return embed


def get_mode_icon(mode: str) -> discord.File:
    path = os.path.join(ASSETS_DIR, f"lfg_{mode}.png")
    return discord.File(path, filename=f"lfg_{mode}.png")


# -- Persistent button view -----------------------------------------------


def build_lfg_view(lfg_id: int, status: str, owner_controls: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)

    is_closed = status == "closed"
    is_full = status == "full"

    if not owner_controls:
        view.add_item(JoinButton(lfg_id, disabled=is_closed or is_full))
        view.add_item(JoinVCButton(lfg_id, disabled=is_closed))
        view.add_item(LeaveButton(lfg_id, disabled=is_closed))

    if owner_controls:
        view.add_item(RemovePlayerButton(lfg_id, disabled=is_closed))
        view.add_item(ChangeVCButton(lfg_id, disabled=is_closed))
        view.add_item(GameFinishedButton(lfg_id, disabled=is_closed))

    return view


# -- DynamicItem buttons ---------------------------------------------------


class JoinButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lfg:join:(?P<id>\d+)"):
    def __init__(self, lfg_id: int, disabled: bool = False, label: str = "Join Game", style: discord.ButtonStyle = discord.ButtonStyle.green, row: int | None = None):
        super().__init__(
            discord.ui.Button(
                label=label,
                style=style,
                custom_id=f"lfg:join:{lfg_id}",
                disabled=disabled,
                row=row,
            )
        )
        self.lfg_id = lfg_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        post = await db.get_lfg(interaction.client.db, self.lfg_id)
        if not post:
            return await interaction.response.send_message("LFG: This game no longer exists.", ephemeral=True)
        if post["status"] != "open":
            return await interaction.response.send_message(f"LFG #{post['guild_seq']}: This game is not open.", ephemeral=True)

        members = await db.get_lfg_members(interaction.client.db, self.lfg_id)
        if interaction.user.id in members:
            return await interaction.response.send_message(f"LFG #{post['guild_seq']}: You already joined.", ephemeral=True)
        if len(members) >= post["max_slots"]:
            return await interaction.response.send_message(f"LFG #{post['guild_seq']}: Party is full.", ephemeral=True)

        added = await db.add_member(interaction.client.db, self.lfg_id, interaction.user.id)
        if not added:
            return await interaction.response.send_message(f"LFG #{post['guild_seq']}: You already joined.", ephemeral=True)
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
    def __init__(self, lfg_id: int, disabled: bool = False, label: str = "Join VC", row: int | None = None):
        super().__init__(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.blurple,
                custom_id=f"lfg:joinvc:{lfg_id}",
                disabled=disabled,
                row=row,
            )
        )
        self.lfg_id = lfg_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        post = await db.get_lfg(interaction.client.db, self.lfg_id)
        if not post:
            return await interaction.response.send_message("LFG: This game no longer exists.", ephemeral=True)
        if not post["voice_channel_id"]:
            return await interaction.response.send_message(f"LFG #{post['guild_seq']}: No voice channel set.", ephemeral=True)

        vc = interaction.guild.get_channel(post["voice_channel_id"])
        if not vc:
            return await interaction.response.send_message(f"LFG #{post['guild_seq']}: Voice channel not found.", ephemeral=True)

        if not interaction.user.voice:
            return await interaction.response.send_message(
                f"LFG #{post['guild_seq']}: You need to be in a voice channel first. Join any VC, then click again.",
                ephemeral=True,
            )

        if interaction.user.voice.channel and interaction.user.voice.channel.id == vc.id:
            return await interaction.response.send_message(
                f"LFG #{post['guild_seq']}: You're already in {vc.name}.",
                ephemeral=True,
            )

        try:
            await interaction.user.move_to(vc)
            await interaction.response.send_message(f"LFG #{post['guild_seq']}: Moved you to {vc.name}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(f"LFG #{post['guild_seq']}: I don't have permission to move you.", ephemeral=True)


class LeaveButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lfg:leave:(?P<id>\d+)"):
    def __init__(self, lfg_id: int, disabled: bool = False, label: str = "Leave Party"):
        super().__init__(
            discord.ui.Button(
                label=label,
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
            return await interaction.response.send_message("LFG: This game no longer exists.", ephemeral=True)
        if interaction.user.id == post["creator_id"]:
            view = CreatorLeaveConfirmView(self.lfg_id, post["guild_seq"])
            return await interaction.response.send_message(
                f"LFG #{post['guild_seq']}: You created this game. Leaving will **delete** the post for everyone. Are you sure?",
                view=view, ephemeral=True,
            )

        removed = await db.remove_member(interaction.client.db, self.lfg_id, interaction.user.id)
        if not removed:
            return await interaction.response.send_message(f"LFG #{post['guild_seq']}: You haven't joined this game.", ephemeral=True)

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


class CreatorLeaveConfirmView(discord.ui.View):
    def __init__(self, lfg_id: int, guild_seq: int):
        super().__init__(timeout=60)
        self.lfg_id = lfg_id
        self.guild_seq = guild_seq

    @discord.ui.button(label="Yes, delete the game", style=discord.ButtonStyle.red)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        post = await db.get_lfg(interaction.client.db, self.lfg_id)
        if not post:
            return await interaction.response.edit_message(
                content=f"LFG #{self.guild_seq}: This game no longer exists.", view=None,
            )

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

        # Clean up in-memory VC tracking state
        cog = interaction.client.get_cog("LFGCog")
        if cog:
            cog._clear_vc_tracking(self.lfg_id)

        await db.delete_lfg(interaction.client.db, self.lfg_id)

        await interaction.response.edit_message(
            content=f"LFG #{self.guild_seq}: Game deleted.", view=None,
        )

        await refresh_board(interaction.client)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=f"LFG #{self.guild_seq}: The creator can't leave. Use the Game Finished button (check your private messages from Let's Raid bot).",
            view=None,
        )


class RemovePlayerButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lfg:kick:(?P<id>\d+)"):
    def __init__(self, lfg_id: int, disabled: bool = False):
        super().__init__(
            discord.ui.Button(
                label="Remove Player",
                style=discord.ButtonStyle.red,
                custom_id=f"lfg:kick:{lfg_id}",
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
            return await interaction.response.send_message("LFG: This game no longer exists.", ephemeral=True)
        if post["status"] == "closed":
            return await interaction.response.send_message(f"LFG #{post['guild_seq']}: This game is already finished.", ephemeral=True)
        if interaction.user.id != post["creator_id"]:
            return await interaction.response.send_message(f"LFG #{post['guild_seq']}: Only the creator can remove players.", ephemeral=True)

        members = await db.get_lfg_members(interaction.client.db, self.lfg_id)
        removable = [uid for uid in members if uid != post["creator_id"]]
        if not removable:
            return await interaction.response.send_message(f"LFG #{post['guild_seq']}: No players to remove.", ephemeral=True)

        guild = interaction.client.get_guild(post["guild_id"])
        options = []
        for uid in removable:
            member = guild.get_member(uid) if guild else None
            name = member.display_name if member else f"Unknown ({uid})"
            options.append(discord.SelectOption(label=name, value=str(uid)))

        view = RemovePlayerView(self.lfg_id, options)
        await interaction.response.send_message("Select a player to remove:", view=view, ephemeral=True)


class RemovePlayerView(discord.ui.View):
    def __init__(self, lfg_id: int, options: list[discord.SelectOption]):
        super().__init__(timeout=60)
        self.lfg_id = lfg_id
        select = discord.ui.Select(placeholder="Choose a player...", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        target_id = int(interaction.data["values"][0])
        post = await db.get_lfg(interaction.client.db, self.lfg_id)
        if not post:
            return await interaction.response.send_message("LFG: This game no longer exists.", ephemeral=True)
        if interaction.user.id != post["creator_id"]:
            return await interaction.response.send_message("Only the creator can remove players.", ephemeral=True)

        removed = await db.remove_member(interaction.client.db, self.lfg_id, target_id)
        if not removed:
            return await interaction.response.send_message(f"LFG #{post['guild_seq']}: That player already left.", ephemeral=True)

        members = await db.get_lfg_members(interaction.client.db, self.lfg_id)

        # Reopen if was full
        status = post["status"]
        if status == "full" and len(members) < post["max_slots"]:
            status = "open"
            await db.update_status(interaction.client.db, self.lfg_id, "open")

        # Update channel post
        post["status"] = status
        guild = interaction.client.get_guild(post["guild_id"])
        if guild:
            channel = guild.get_channel(post["channel_id"])
            if channel:
                try:
                    msg = await channel.fetch_message(post["message_id"])
                    embed = build_lfg_embed(post, members, guild)
                    view = build_lfg_view(self.lfg_id, status)
                    await msg.edit(embed=embed, view=view, attachments=[get_mode_icon(post["mode"])])
                except discord.NotFound:
                    pass

        target = guild.get_member(target_id) if guild else None
        target_name = target.display_name if target else f"User {target_id}"
        await interaction.response.edit_message(content=f"Removed **{target_name}** from LFG #{post['guild_seq']}.", view=None)

        await refresh_board(interaction.client)
        if guild:
            await update_vc_status(interaction.client, post, guild)


class ChangeVCButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lfg:changevc:(?P<id>\d+)"):
    def __init__(self, lfg_id: int, disabled: bool = False):
        super().__init__(
            discord.ui.Button(
                label="Change VC",
                style=discord.ButtonStyle.grey,
                custom_id=f"lfg:changevc:{lfg_id}",
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
            return await interaction.response.send_message("LFG: This game no longer exists.", ephemeral=True)
        if post["status"] == "closed":
            return await interaction.response.send_message(f"LFG #{post['guild_seq']}: This game is already finished.", ephemeral=True)
        if interaction.user.id != post["creator_id"]:
            return await interaction.response.send_message(f"LFG #{post['guild_seq']}: Only the creator can change the VC.", ephemeral=True)

        guild = interaction.client.get_guild(post["guild_id"])
        if not guild:
            return await interaction.response.send_message("Could not find the server.", ephemeral=True)

        channels = get_vc_channels(guild)
        if not channels:
            return await interaction.response.send_message("No voice channels found.", ephemeral=True)

        options = [
            discord.SelectOption(
                label=f"{ch.name} ({len(ch.members)} in channel)",
                value=str(ch.id),
                default=ch.id == post["voice_channel_id"],
            )
            for ch in channels[:25]  # Select menus allow max 25 options
        ]

        view = ChangeVCView(self.lfg_id, options)
        await interaction.response.send_message("Select a new voice channel:", view=view, ephemeral=True)


class ChangeVCView(discord.ui.View):
    def __init__(self, lfg_id: int, options: list[discord.SelectOption]):
        super().__init__(timeout=60)
        self.lfg_id = lfg_id
        select = discord.ui.Select(placeholder="Choose a voice channel...", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        new_vc_id = int(interaction.data["values"][0])
        post = await db.get_lfg(interaction.client.db, self.lfg_id)
        if not post:
            return await interaction.response.send_message("LFG: This game no longer exists.", ephemeral=True)
        if interaction.user.id != post["creator_id"]:
            return await interaction.response.send_message("Only the creator can change the VC.", ephemeral=True)

        old_vc_id = post["voice_channel_id"]

        # Reject if the target VC is already attached to another active post
        if new_vc_id != old_vc_id:
            taken = await db.get_active_post_by_vc(
                interaction.client.db, post["guild_id"], new_vc_id, exclude_lfg_id=self.lfg_id
            )
            if taken:
                return await interaction.response.send_message(
                    format_vc_taken_message(f"<#{new_vc_id}>", taken, interaction.guild),
                    ephemeral=True,
                )

        # Update DB
        await db.update_voice_channel(interaction.client.db, self.lfg_id, new_vc_id)
        post["voice_channel_id"] = new_vc_id

        guild = interaction.client.get_guild(post["guild_id"])
        if guild:
            # Clear old VC status
            if old_vc_id and old_vc_id != new_vc_id:
                old_post = dict(post, voice_channel_id=old_vc_id, status="closed")
                await update_vc_status(interaction.client, old_post, guild)

            # Update channel post
            members = await db.get_lfg_members(interaction.client.db, self.lfg_id)
            channel = guild.get_channel(post["channel_id"])
            if channel:
                try:
                    msg = await channel.fetch_message(post["message_id"])
                    embed = build_lfg_embed(post, members, guild)
                    view = build_lfg_view(self.lfg_id, post["status"])
                    await msg.edit(embed=embed, view=view, attachments=[get_mode_icon(post["mode"])])
                except discord.NotFound:
                    pass

            # Set new VC status
            await update_vc_status(interaction.client, post, guild)

        new_vc = guild.get_channel(new_vc_id) if guild else None
        vc_name = new_vc.name if new_vc else str(new_vc_id)
        await interaction.response.edit_message(content=f"Voice channel changed to **{vc_name}** for LFG #{post['guild_seq']}.", view=None)

        await refresh_board(interaction.client)


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
            return await interaction.response.send_message("LFG: This game no longer exists.", ephemeral=True)
        if interaction.user.id != post["creator_id"]:
            return await interaction.response.send_message(f"LFG #{post['guild_seq']}: Only the creator can finish this.", ephemeral=True)

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

        # Clean up in-memory VC tracking state
        cog = interaction.client.get_cog("LFGCog")
        if cog:
            cog._clear_vc_tracking(self.lfg_id)

        await db.delete_lfg(interaction.client.db, self.lfg_id)

        # Update the DM message
        await interaction.response.edit_message(
            content=f"LFG #{post['guild_seq']}: Game finished. Post removed.",
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
                label=f"Start looking for {mode_label}",
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
        blocked = await _check_active_game(interaction)
        if not blocked:
            await interaction.response.send_modal(LFGModal("pvp", interaction.guild))

    @discord.ui.button(label="PvE", style=discord.ButtonStyle.green, custom_id="lfgstart:pve")
    async def pve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        blocked = await _check_active_game(interaction)
        if not blocked:
            await interaction.response.send_modal(LFGModal("pve", interaction.guild))


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

    def __init__(self, mode_value: str, guild: discord.Guild):
        super().__init__()
        self.mode_value = mode_value

        channels = get_vc_channels(guild)
        options = [discord.SelectOption(label="Automatic (best guess)", value="auto", default=True)]
        options += [
            discord.SelectOption(
                label=f"{ch.name} ({len(ch.members)} {'participant' if len(ch.members) == 1 else 'participants'} currently)",
                value=str(ch.id),
            )
            for ch in channels[:24]  # Select menus allow max 25 options (1 used by "Automatic")
        ]

        self.vc_select = discord.ui.Select(
            placeholder="Select a voice channel",
            options=options,
            min_values=1,
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

        # Find LFG channel by ID or name
        if config.LFG_CHANNEL.isdigit():
            lfg_channel = interaction.guild.get_channel(int(config.LFG_CHANNEL))
        else:
            lfg_channel = discord.utils.get(
                interaction.guild.text_channels, name=config.LFG_CHANNEL
            )
        if not lfg_channel:
            return await interaction.response.send_message(
                f"Could not find LFG channel ({config.LFG_CHANNEL}).", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        # Resolve voice channel: user's selection > current VC > least-full VC* channel
        voice_channel = None
        vc_warning = None

        if self.vc_select.values and self.vc_select.values[0] not in ("auto", "none"):
            voice_channel = interaction.guild.get_channel(int(self.vc_select.values[0]))
        elif interaction.user.voice and interaction.user.voice.channel:
            voice_channel = interaction.user.voice.channel
        else:
            voice_channel = find_least_full_voice_channel(interaction.guild)
            if voice_channel and len(voice_channel.members) > 0:
                vc_warning = f"Note: {voice_channel.name} has {len(voice_channel.members)} participant(s) already."

        # Enforce VC uniqueness across active posts
        if voice_channel:
            taken = await db.get_active_post_by_vc(
                interaction.client.db, interaction.guild.id, voice_channel.id
            )
            if taken:
                # Response was already deferred above, so use followup.
                await interaction.followup.send(
                    format_vc_taken_message(voice_channel.mention, taken, interaction.guild),
                    ephemeral=True,
                )
                return

        # Find the role to ping
        role_name = config.LFG_ROLE_NAMES.get(self.mode_value)
        role = discord.utils.get(interaction.guild.roles, name=role_name) if role_name else None

        description = self.description_input.value or None
        start_time = self.start_time_input.value or None

        # Insert into DB first (with placeholder message_id) to get the real LFG ID
        lfg_id, guild_seq = await db.create_lfg(
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
            "guild_seq": guild_seq,
            "creator_id": interaction.user.id,
            "voice_channel_id": voice_channel.id if voice_channel else None,
            "mode": self.mode_value,
            "description": description,
            "start_time": start_time,
            "max_slots": max_slots,
            "status": "open",
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }
        members = [interaction.user.id]
        embed = build_lfg_embed(post, members, interaction.guild)
        channel_view = build_lfg_view(lfg_id, "open")

        # Send once with everything -- no edit needed
        ping_content = role.mention if role else None
        try:
            msg = await lfg_channel.send(
                content=ping_content, embed=embed, view=channel_view,
                file=get_mode_icon(self.mode_value),
            )
        except discord.Forbidden:
            await db.delete_lfg(interaction.client.db, lfg_id)
            await interaction.followup.send(
                f"I don't have permission to post in {lfg_channel.mention}.", ephemeral=True
            )
            return

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
        # VC status is set lazily when someone first joins the voice channel
        # (via on_voice_state_update), not eagerly on creation.

        if vc_warning:
            await interaction.followup.send(vc_warning, ephemeral=True)
        else:
            try:
                await interaction.delete_original_response()
            except discord.NotFound:
                pass


# -- Helpers ---------------------------------------------------------------


def format_vc_taken_message(vc_mention: str, taken: dict, guild: discord.Guild) -> str:
    """Build a debug-friendly rejection message when a VC is already attached to a post."""
    creator = guild.get_member(taken["creator_id"])
    creator_name = creator.display_name if creator else f"User {taken['creator_id']}"
    created_at = taken.get("created_at")
    when = ""
    if created_at:
        dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        when = f", created <t:{int(dt.timestamp())}:R>"
    return (
        f"{vc_mention} is already attached to LFG #{taken['guild_seq']} "
        f"(host: {creator_name}, status: {taken['status']}{when}). "
        "Pick a different voice channel."
    )


def get_vc_channels(guild: discord.Guild) -> list[discord.VoiceChannel]:
    hidden = config.HIDDEN_VC.get(guild.id, set())
    channels = [ch for ch in guild.voice_channels if ch.id not in hidden]
    # VC* channels first, then everything else; alphabetical within each group
    return sorted(channels, key=lambda c: (not c.name.startswith("VC"), c.name.lower()))


def find_least_full_voice_channel(guild: discord.Guild) -> discord.VoiceChannel | None:
    channels = get_vc_channels(guild)
    if not channels:
        return None
    return min(channels, key=lambda ch: len(ch.members))


async def _check_active_game(interaction: discord.Interaction) -> bool:
    """If user is in an active game, send ephemeral and return True (blocked)."""
    active = await db.get_active_post_for_user(interaction.client.db, interaction.guild.id, interaction.user.id)
    if not active:
        return False
    seq = active["guild_seq"]
    if active["creator_id"] == interaction.user.id:
        await interaction.response.send_message(
            f"You already have an open game (LFG #{seq}). Finish it before creating another.",
            ephemeral=True,
        )
    else:
        view = discord.ui.View(timeout=None)
        view.add_item(LeaveButton(active["id"], label=f"Leave #{seq}"))
        await interaction.response.send_message(
            f"You're already in a game (LFG #{seq}). Leave it first to create your own.",
            view=view, ephemeral=True,
        )
    return True


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
        embed.description = "No active games right now. Use `/lfg` to start one!\nUse `/lfgstatus` to change your notification settings."
        return embed

    for post in posts:
        mode_label = "PvP" if post["mode"] == "pvp" else "PvE"
        emoji = get_lfg_emoji(guild, post["mode"])
        members = await db.get_lfg_members(bot_db, post["id"])
        prefix = f"{emoji} " if emoji else ""
        title = f"{prefix}#{post['guild_seq']} - {mode_label}"
        if post["description"]:
            title += f": {post['description'][:50]}"

        jump_url = f"https://discord.com/channels/{guild.id}/{post['channel_id']}/{post['message_id']}"
        lines = [f"[Jump to post]({jump_url})"]
        party_str = f"Players: {len(members)}/{post['max_slots']}"
        if post["status"] == "full":
            party_str += " (**game is full** :no_entry: )"
        else:
            party_str += " (**open** :white_check_mark:)"
        lines.append(party_str)
        if post["start_time"]:
            start_str = post["start_time"]
            if post.get("created_at"):
                dt = datetime.strptime(post["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                ts = int(dt.timestamp())
                start_str += f" (<t:{ts}:R>)"
            lines.append(f"Start: {start_str}")
        if post["voice_channel_id"]:
            lines.append(f"VC: <#{post['voice_channel_id']}>")

        creator = guild.get_member(post["creator_id"])
        creator_name = creator.display_name if creator else "Unknown"
        lines.append(f"Host: {creator_name}")

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
            status_parts = [mode_label]
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


_VC_PLAYED_THRESHOLD = 10 * 60  # seconds creator must be in VC before auto-clear
_VC_SESSION_THRESHOLD = 60 * 60  # 1 hour of 2+ people in VC -> early expiry
_EARLY_EXPIRY_HOURS = 3  # shortened cleanup window after a VC session
_DEFAULT_EXPIRY_HOURS = 12


class LFGCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._creator_vc_joins: dict[int, float] = {}  # lfg_id -> monotonic join time
        self._vc_game_played: set[int] = set()  # lfg_ids where creator was in 10+ min
        self._vc_multi_start: dict[int, float] = {}  # lfg_id -> when 2+ members first in VC together
        self._vc_early_expiry: set[int] = set()  # lfg_ids qualifying for 3h expiry

    async def cog_load(self):
        self.bot.add_dynamic_items(
            JoinButton, JoinVCButton, LeaveButton, RemovePlayerButton, ChangeVCButton, GameFinishedButton, RoleToggleButton
        )
        self.cleanup_old_posts.start()
        log.info("LFG cog loaded")

    async def cog_unload(self):
        self.bot.remove_dynamic_items(
            JoinButton, JoinVCButton, LeaveButton, RemovePlayerButton, ChangeVCButton, GameFinishedButton, RoleToggleButton
        )
        self.cleanup_old_posts.cancel()

    def _clear_vc_tracking(self, post_id: int):
        """Remove all in-memory VC tracking state for a post."""
        self._creator_vc_joins.pop(post_id, None)
        self._vc_game_played.discard(post_id)
        self._vc_multi_start.pop(post_id, None)
        self._vc_early_expiry.discard(post_id)

    async def _close_post(self, post):
        """Mark a post closed: update DB, delete the channel message, clear VC status, drop tracking."""
        try:
            await db.update_status(self.bot.db, post["id"], "closed")
            guild = self.bot.get_guild(post["guild_id"])
            if not guild:
                self._clear_vc_tracking(post["id"])
                return
            channel = guild.get_channel(post["channel_id"])
            if channel:
                try:
                    msg = await channel.fetch_message(post["message_id"])
                    await msg.delete()
                except discord.NotFound:
                    # Channel message is already gone — drop the row entirely
                    await db.delete_lfg(self.bot.db, post["id"])
                    self._clear_vc_tracking(post["id"])
                    return
            post_dict = dict(post)
            post_dict["status"] = "closed"
            await update_vc_status(self.bot, post_dict, guild)
            self._clear_vc_tracking(post["id"])
        except Exception:
            log.exception("Error closing LFG post %s", post["id"])

    def _update_vc_session_tracking(self, post_id: int, creator_id: int, channel: discord.VoiceChannel):
        """Track whether 2+ people (including creator) are in the VC together."""
        if post_id in self._vc_early_expiry:
            return  # already qualified

        creator_present = any(m.id == creator_id for m in channel.members)
        has_multiple = len(channel.members) >= 2

        if creator_present and has_multiple:
            # Start timer if not already running
            if post_id not in self._vc_multi_start:
                self._vc_multi_start[post_id] = time.monotonic()
        else:
            # Condition broke — qualify for early expiry if threshold was reached during the session
            start = self._vc_multi_start.pop(post_id, None)
            if start is not None and time.monotonic() - start >= _VC_SESSION_THRESHOLD:
                self._vc_early_expiry.add(post_id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        # Only care about channel changes (join/leave/move), not mute/deafen
        if before.channel == after.channel:
            return

        joined = after.channel
        left = before.channel

        # Someone joined a VC — check if it's linked to an active LFG post
        if joined:
            post = await db.get_active_post_by_vc(self.bot.db, member.guild.id, joined.id)
            if post:
                if member.id == post["creator_id"]:
                    self._creator_vc_joins[post["id"]] = time.monotonic()
                self._update_vc_session_tracking(post["id"], post["creator_id"], joined)

                # Any join into the linked VC sets the status (covers the case
                # where non-members were already in the VC at LFG creation time).
                # vc.edit is idempotent so re-setting during a session is harmless.
                post_dict = dict(post)
                await update_vc_status(self.bot, post_dict, member.guild)

        # Someone left a VC
        if left:
            post = await db.get_active_post_by_vc(self.bot.db, member.guild.id, left.id)
            if not post:
                return

            # Creator left — check if they were in long enough
            if member.id == post["creator_id"] and post["id"] in self._creator_vc_joins:
                join_time = self._creator_vc_joins.pop(post["id"])
                if time.monotonic() - join_time >= _VC_PLAYED_THRESHOLD:
                    self._vc_game_played.add(post["id"])

            # Update session tracking (member count changed)
            self._update_vc_session_tracking(post["id"], post["creator_id"], left)

            # VC is now empty and game was played — close the post and free the VC
            if len(left.members) == 0 and post["id"] in self._vc_game_played:
                await self._close_post(post)
                await refresh_board(self.bot)

    @app_commands.command(name="lfg", description="Create a Looking For Game post")
    @app_commands.describe(mode="PvP or PvE")
    @app_commands.choices(mode=[
        app_commands.Choice(name="pvp", value="pvp"),
        app_commands.Choice(name="pve", value="pve"),
    ])
    async def lfg(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        if await _check_active_game(interaction):
            return
        await interaction.response.send_modal(LFGModal(mode.value, interaction.guild))

    @app_commands.command(name="lfgsetup", description="Post the role picker and live game board in this channel")
    @app_commands.checks.has_permissions(manage_roles=True, manage_channels=True)
    async def lfgsetup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # Post role picker
        role_embed = discord.Embed(
            title="Are you looking for a game?",
            description="Choose the type of game you want to be notified for. You can choose one, both, or neither to disable pings.\n\nOr start your own game: `/lfg pvp` or `/lfg pve`.",
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

        await interaction.delete_original_response()

    @app_commands.command(name="lfgstatus", description="Change your looking for game settings")
    async def lfgstatus(self, interaction: discord.Interaction):
        view = LFGNowView(interaction.user)
        await interaction.response.send_message(
            "Set your game status:", view=view, ephemeral=True
        )

    @app_commands.command(name="lfglist", description="Show all active games")
    async def lfglist(self, interaction: discord.Interaction):
        embed = await build_board_embed(interaction.client.db, interaction.guild)
        posts = await db.get_open_posts(interaction.client.db, interaction.guild.id)

        view = discord.ui.View(timeout=None)
        # Pad labels to consistent width using en spaces (\u2002)
        max_seq_len = max((len(str(p["guild_seq"])) for p in posts), default=1)
        for i, post in enumerate(posts[:5]):
            if post["status"] != "closed":
                is_full = post["status"] == "full"
                join_style = discord.ButtonStyle.grey if is_full else discord.ButtonStyle.green
                padded_seq = str(post["guild_seq"]).ljust(max_seq_len, "\u2002")
                view.add_item(JoinButton(post["id"], disabled=is_full, label=f"Join #{padded_seq}", style=join_style))
                if post["voice_channel_id"]:
                    view.add_item(JoinVCButton(post["id"], label=f"Join #{padded_seq} VC"))

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

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
        embed.set_footer(text="Tip: Use /lfgstatus to change roles -- editing roles manually may cause buttons to go out of sync (they will self-correct eventually).")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tasks.loop(minutes=5)
    async def cleanup_old_posts(self):
        # Posts with a detected VC session expire after 3h, others after 12h
        candidates = await db.get_expired_posts(self.bot.db, hours=_EARLY_EXPIRY_HOURS)
        default = await db.get_expired_posts(self.bot.db, hours=_DEFAULT_EXPIRY_HOURS)
        default_ids = {p["id"] for p in default}
        expired = list(default)
        for p in candidates:
            if p["id"] not in default_ids and p["id"] in self._vc_early_expiry:
                expired.append(p)
        for post in expired:
            await self._close_post(post)
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
