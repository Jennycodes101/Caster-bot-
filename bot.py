import asyncio
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


DB_PATH = os.getenv("CASTER_BOT_DB", "caster_bot.db")
COOLDOWN_HOURS = 15


@dataclass
class CastRequest:
    request_id: int
    guild_id: int
    channel_id: int
    activity_message_id: Optional[int]
    requester_id: int
    event_type: str
    event_time: str
    additional_notes: str
    status: str
    time_requested: str
    time_assigned: Optional[str]
    assigned_caster_id: Optional[int]


class CasterDB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                request_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                activity_message_id INTEGER,
                requester_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                event_time TEXT NOT NULL,
                additional_notes TEXT,
                status TEXT NOT NULL,
                time_requested TEXT NOT NULL,
                time_assigned TEXT,
                assigned_caster_id INTEGER
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cooldowns (
                user_id INTEGER PRIMARY KEY,
                last_request_time TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS availability (
                request_id INTEGER NOT NULL,
                caster_id INTEGER NOT NULL,
                available INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (request_id, caster_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ready_casters (
                caster_id INTEGER PRIMARY KEY,
                is_ready INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS assignment_history (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL,
                requester_id INTEGER NOT NULL,
                assigned_caster_id INTEGER,
                event_type TEXT NOT NULL,
                event_time TEXT NOT NULL,
                time_requested TEXT NOT NULL,
                time_assigned TEXT,
                completion_status TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    @staticmethod
    def utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def create_request(self, guild_id: int, channel_id: int, requester_id: int, event_type: str, event_time: str, notes: str) -> CastRequest:
        now = self.utc_now()
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO requests (guild_id, channel_id, requester_id, event_type, event_time, additional_notes, status, time_requested)
            VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)
            """,
            (guild_id, channel_id, requester_id, event_type, event_time, notes, now),
        )
        request_id = cur.lastrowid
        self.conn.commit()
        return self.get_request(request_id)

    def get_request(self, request_id: int) -> Optional[CastRequest]:
        row = self.conn.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()
        if row is None:
            return None
        return CastRequest(**dict(row))

    def set_activity_message(self, request_id: int, message_id: int) -> None:
        self.conn.execute("UPDATE requests SET activity_message_id = ? WHERE request_id = ?", (message_id, request_id))
        self.conn.commit()

    def set_cooldown(self, user_id: int) -> None:
        self.conn.execute(
            """
            INSERT INTO cooldowns (user_id, last_request_time) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET last_request_time = excluded.last_request_time
            """,
            (user_id, self.utc_now()),
        )
        self.conn.commit()

    def get_last_request_time(self, user_id: int) -> Optional[datetime]:
        row = self.conn.execute("SELECT last_request_time FROM cooldowns WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row["last_request_time"])

    def update_availability(self, request_id: int, caster_id: int, available: bool) -> None:
        self.conn.execute(
            """
            INSERT INTO availability (request_id, caster_id, available, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(request_id, caster_id) DO UPDATE SET available = excluded.available, updated_at = excluded.updated_at
            """,
            (request_id, caster_id, int(available), self.utc_now()),
        )
        if available:
            self.conn.execute(
                """
                UPDATE requests SET status = 'WAITING'
                WHERE request_id = ? AND status = 'PENDING'
                """,
                (request_id,),
            )
        else:
            still_available = self.count_available(request_id)
            if still_available == 0:
                self.conn.execute(
                    """
                    UPDATE requests SET status = 'PENDING'
                    WHERE request_id = ? AND status = 'WAITING'
                    """,
                    (request_id,),
                )
        self.conn.commit()

    def count_available(self, request_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM availability WHERE request_id = ? AND available = 1", (request_id,)
        ).fetchone()
        return int(row["c"])

    def set_ready(self, caster_id: int, ready: bool) -> None:
        self.conn.execute(
            """
            INSERT INTO ready_casters (caster_id, is_ready, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(caster_id) DO UPDATE SET is_ready = excluded.is_ready, updated_at = excluded.updated_at
            """,
            (caster_id, int(ready), self.utc_now()),
        )
        self.conn.commit()

    def is_ready(self, caster_id: int) -> bool:
        row = self.conn.execute("SELECT is_ready FROM ready_casters WHERE caster_id = ?", (caster_id,)).fetchone()
        return bool(row and row["is_ready"])

    def count_ready(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM ready_casters WHERE is_ready = 1").fetchone()
        return int(row["c"])

    def get_waitlist(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT request_id, requester_id, event_time, status
            FROM requests
            WHERE status = 'WAITING'
            ORDER BY datetime(time_requested) ASC, request_id ASC
            """
        ).fetchall()

    def request_waitlist_position(self, request_id: int) -> Optional[int]:
        waitlist = self.get_waitlist()
        for idx, row in enumerate(waitlist, 1):
            if row["request_id"] == request_id:
                return idx
        return None

    def assign_oldest_waiting_for_caster(self, caster_id: int) -> Optional[CastRequest]:
        row = self.conn.execute(
            """
            SELECT r.request_id
            FROM requests r
            INNER JOIN availability a ON a.request_id = r.request_id
            WHERE r.status = 'WAITING' AND a.available = 1 AND a.caster_id = ?
            ORDER BY datetime(r.time_requested) ASC, r.request_id ASC
            LIMIT 1
            """,
            (caster_id,),
        ).fetchone()
        if row is None:
            return None
        return self.assign_request(row["request_id"], caster_id)

    def assign_request(self, request_id: int, caster_id: int) -> Optional[CastRequest]:
        req = self.get_request(request_id)
        if req is None or req.status not in {"WAITING", "PENDING"}:
            return None
        assigned_at = self.utc_now()
        self.conn.execute(
            """
            UPDATE requests
            SET status = 'CONFIRMED', time_assigned = ?, assigned_caster_id = ?
            WHERE request_id = ?
            """,
            (assigned_at, caster_id, request_id),
        )
        self.conn.execute(
            """
            INSERT INTO assignment_history
            (request_id, requester_id, assigned_caster_id, event_type, event_time, time_requested, time_assigned, completion_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'CONFIRMED')
            """,
            (req.request_id, req.requester_id, caster_id, req.event_type, req.event_time, req.time_requested, assigned_at),
        )
        self.conn.commit()
        return self.get_request(request_id)

    def close_request(self, request_id: int, completion_status: str) -> Optional[CastRequest]:
        req = self.get_request(request_id)
        if req is None:
            return None
        self.conn.execute("UPDATE requests SET status = ? WHERE request_id = ?", (completion_status.upper(), request_id))
        self.conn.execute(
            """
            INSERT INTO assignment_history
            (request_id, requester_id, assigned_caster_id, event_type, event_time, time_requested, time_assigned, completion_status, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                req.request_id,
                req.requester_id,
                req.assigned_caster_id,
                req.event_type,
                req.event_time,
                req.time_requested,
                req.time_assigned,
                completion_status.upper(),
                self.utc_now(),
            ),
        )
        self.conn.commit()
        return self.get_request(request_id)

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_setting(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


class CasterRequestView(discord.ui.View):
    def __init__(self, db: CasterDB, bot: "CasterBot"):
        super().__init__(timeout=None)
        self.db = db
        self.bot = bot

    async def _set_availability(self, interaction: discord.Interaction, available: bool) -> None:
        request_id = extract_request_id(interaction.message)
        if request_id is None:
            await interaction.response.send_message("Unable to detect request ID.", ephemeral=True)
            return
        
        # Check if user has Caster role
        user_is_caster = False
        if isinstance(interaction.user, discord.Member):
            role_id = os.getenv("CASTER_ROLE_ID")
            if role_id:
                user_is_caster = any(role.id == int(role_id) for role in interaction.user.roles)
            else:
                user_is_caster = any(role.name.lower() == "caster" for role in interaction.user.roles)
        
        if not user_is_caster:
            await interaction.response.send_message("Only users with the Caster role can use this.", ephemeral=True)
            return

        before = self.db.get_request(request_id)
        self.db.update_availability(request_id, interaction.user.id, available)

        req = self.db.get_request(request_id)
        if req and before and before.status != "WAITING" and req.status == "WAITING":
            position = self.db.request_waitlist_position(request_id)
            if position:
                await interaction.channel.send(
                    f"📋 Request #{request_id} is now in waitlist position **{position}** for <@{req.requester_id}>."
                )

        action = "available" if available else "unavailable"
        await interaction.response.send_message(f"You are now marked as **{action}** for request #{request_id}.", ephemeral=True)
        
        # If caster said YES (available), DM the requester
        if available and req:
            requester = self.bot.get_user(req.requester_id)
            if requester:
                try:
                    await requester.send(f"✅ You are being casted by <@{interaction.user.id}>!")
                except discord.Forbidden:
                    pass

    @discord.ui.button(label="✅ Available", style=discord.ButtonStyle.success, custom_id="caster_request_available")
    async def available(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._set_availability(interaction, True)

    @discord.ui.button(label="❌ Unavailable", style=discord.ButtonStyle.danger, custom_id="caster_request_unavailable")
    async def unavailable(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._set_availability(interaction, False)


class ReadyCasterView(discord.ui.View):
    def __init__(self, db: CasterDB):
        super().__init__(timeout=None)
        self.db = db

    @discord.ui.button(label="🎙️ Ready to Cast (Toggle)", style=discord.ButtonStyle.primary, custom_id="caster_ready_toggle")
    async def toggle_ready(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        # Check if user has Caster role
        user_is_caster = False
        if isinstance(interaction.user, discord.Member):
            role_id = os.getenv("CASTER_ROLE_ID")
            if role_id:
                user_is_caster = any(role.id == int(role_id) for role in interaction.user.roles)
            else:
                user_is_caster = any(role.name.lower() == "caster" for role in interaction.user.roles)
        
        if not user_is_caster:
            await interaction.response.send_message("Only users with the Caster role can toggle readiness.", ephemeral=True)
            return

        currently_ready = self.db.is_ready(interaction.user.id)
        if currently_ready:
            self.db.set_ready(interaction.user.id, False)
            await interaction.response.send_message("You are now marked as **Not Ready**.", ephemeral=True)
            return

        self.db.set_ready(interaction.user.id, True)
        assigned = self.db.assign_oldest_waiting_for_caster(interaction.user.id)

        if assigned is None:
            await interaction.response.send_message(
                "You are marked **Ready**. No matching waitlist request is available yet.", ephemeral=True
            )
            return

        self.db.set_ready(interaction.user.id, False)
        await interaction.response.send_message(f"Assigned request #{assigned.request_id} to you.", ephemeral=True)


def extract_request_id(message: Optional[discord.Message]) -> Optional[int]:
    if message is None or not message.embeds:
        return None
    embed = message.embeds[0]
    for field in embed.fields:
        if field.name.strip().lower() == "request id":
            try:
                return int(field.value.strip().replace("#", ""))
            except ValueError:
                return None
    return None


class CasterBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.db = CasterDB(DB_PATH)

    async def setup_hook(self) -> None:
        # Create view instances with database reference
        self.request_view = CasterRequestView(self.db, self)
        self.ready_view = ReadyCasterView(self.db)
        
        # Add views for persistent button interactions
        self.add_view(self.request_view)
        self.add_view(self.ready_view)
        
        guild_id = os.getenv("DISCORD_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def ensure_caster_requests_channel(self, guild: discord.Guild) -> discord.TextChannel:
        """Ensure the caster-requests channel exists"""
        saved_id = self.db.get_setting(f"caster_requests_channel:{guild.id}")
        if saved_id:
            existing = guild.get_channel(int(saved_id))
            if isinstance(existing, discord.TextChannel):
                return existing

        channel = discord.utils.get(guild.text_channels, name="caster-requests")
        if channel is None:
            bot_member = guild.me or guild.get_member(self.user.id)  # type: ignore[arg-type]
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
            }
            if bot_member is not None:
                overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
            channel = await guild.create_text_channel("caster-requests", overwrites=overwrites)
        self.db.set_setting(f"caster_requests_channel:{guild.id}", str(channel.id))
        return channel

    async def ensure_log_channel(self, guild: discord.Guild) -> discord.TextChannel:
        saved_id = self.db.get_setting(f"log_channel:{guild.id}")
        if saved_id:
            existing = guild.get_channel(int(saved_id))
            if isinstance(existing, discord.TextChannel):
                return existing

        channel = discord.utils.get(guild.text_channels, name="caster-request-logs")
        if channel is None:
            bot_member = guild.me or guild.get_member(self.user.id)  # type: ignore[arg-type]
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
            }
            if bot_member is not None:
                overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
            channel = await guild.create_text_channel("caster-request-logs", overwrites=overwrites)
        self.db.set_setting(f"log_channel:{guild.id}", str(channel.id))
        return channel

    async def log_event(self, guild: discord.Guild, title: str, req: CastRequest, completion_status: Optional[str] = None) -> None:
        log_channel = await self.ensure_log_channel(guild)
        embed = build_request_embed(req, self.db.count_available(req.request_id), title=title)
        embed.add_field(name="Time Requested", value=req.time_requested, inline=False)
        embed.add_field(name="Time Assigned", value=req.time_assigned or "Not assigned", inline=False)
        embed.add_field(name="Completion Status", value=completion_status or req.status, inline=False)
        await log_channel.send(embed=embed)

    async def refresh_request_message(self, request_id: int) -> None:
        req = self.db.get_request(request_id)
        if req is None or req.activity_message_id is None:
            return
        guild = self.get_guild(req.guild_id)
        if guild is None:
            return
        # Get the caster-requests channel
        try:
            caster_requests_channel = await self.ensure_caster_requests_channel(guild)
        except Exception:
            return
        try:
            message = await caster_requests_channel.fetch_message(req.activity_message_id)
        except (discord.NotFound, discord.Forbidden):
            return
        embed = build_request_embed(req, self.db.count_available(req.request_id))
        await message.edit(embed=embed, view=self.request_view)

    async def send_assignment_notifications(self, guild: Optional[discord.Guild], req: CastRequest) -> None:
        if guild is None:
            return
        # Send notifications to the caster-requests channel
        try:
            caster_requests_channel = await self.ensure_caster_requests_channel(guild)
        except Exception:
            return

        caster_mention = f"<@{req.assigned_caster_id}>" if req.assigned_caster_id else "Unknown"
        requester_mention = f"<@{req.requester_id}>"
        embed = discord.Embed(title="Caster Assigned", color=discord.Color.gold())
        embed.add_field(name="Caster", value=caster_mention, inline=False)
        embed.add_field(name="Requester", value=requester_mention, inline=False)
        embed.add_field(name="Event Type", value=req.event_type, inline=False)
        embed.add_field(name="Event Time", value=req.event_time, inline=False)
        await caster_requests_channel.send(content=f"{caster_mention} {requester_mention}", embed=embed)

        requester = guild.get_member(req.requester_id)
        if requester:
            try:
                await requester.send(
                    f"🎉 A caster has been assigned to your request #{req.request_id}! ({caster_mention})"
                )
            except discord.Forbidden:
                pass
        await self.log_event(guild, "Request Assigned", req)


def build_request_embed(req: CastRequest, available_count: int, *, title: str = "Caster Activity Check") -> discord.Embed:
    status_display = req.status.capitalize()
    embed = discord.Embed(title=title, color=discord.Color.blurple())
    embed.add_field(name="Event Type", value=req.event_type, inline=False)
    embed.add_field(name="Event Time", value=req.event_time, inline=False)
    embed.add_field(name="Requesting User", value=f"<@{req.requester_id}>", inline=False)
    embed.add_field(name="Additional Notes", value=req.additional_notes or "None", inline=False)
    embed.add_field(name="Request ID", value=f"#{req.request_id}", inline=True)
    embed.add_field(name="Available Casters", value=str(available_count), inline=True)
    embed.add_field(name="Request Status", value=status_display, inline=True)
    if req.assigned_caster_id:
        embed.add_field(name="Assigned Caster", value=f"<@{req.assigned_caster_id}>", inline=False)
    embed.set_footer(text="Caster Queue • Esports Broadcast Desk")
    return embed


bot = CasterBot()


@bot.tree.command(name="requestcast", description="Request a caster for an esports match")
@app_commands.describe(event_type="Type of event", event_time="Scheduled event time", additional_notes="Optional details")
async def requestcast(
    interaction: discord.Interaction, event_type: str, event_time: str, additional_notes: Optional[str] = ""
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    last_used = bot.db.get_last_request_time(interaction.user.id)
    if last_used:
        now = datetime.now(timezone.utc)
        elapsed = now - last_used
        cooldown = timedelta(hours=COOLDOWN_HOURS)
        if elapsed < cooldown:
            remaining = cooldown - elapsed
            total_minutes = int(remaining.total_seconds() // 60)
            hours = total_minutes // 60
            minutes = total_minutes % 60
            await interaction.response.send_message(
                f"You must wait {hours} hours and {minutes} minutes before requesting another caster.",
                ephemeral=True,
            )
            return

    # Get the caster-requests channel
    try:
        caster_requests_channel = await bot.ensure_caster_requests_channel(interaction.guild)
    except Exception as e:
        await interaction.response.send_message(f"Error creating caster-requests channel: {e}", ephemeral=True)
        return

    req = bot.db.create_request(
        interaction.guild.id,
        caster_requests_channel.id,
        interaction.user.id,
        event_type.strip(),
        event_time.strip(),
        (additional_notes or "").strip(),
    )

    role_id = os.getenv("CASTER_ROLE_ID")
    role_ping = f"<@&{role_id}>" if role_id else "@Caster"
    embed = build_request_embed(req, available_count=0)
    activity_message = await caster_requests_channel.send(content=f"{role_ping} New caster request!", embed=embed, view=bot.request_view)
    bot.db.set_activity_message(req.request_id, activity_message.id)
    bot.db.set_cooldown(interaction.user.id)

    await interaction.response.send_message(
        f"✅ Your caster request has been submitted. Request ID: **#{req.request_id}**\nPosted in <#{caster_requests_channel.id}>", ephemeral=True
    )
    await bot.log_event(interaction.guild, "Request Created", req)


@bot.tree.command(name="waitlist", description="View the current caster request waitlist")
@app_commands.checks.has_permissions(manage_guild=True)
async def waitlist(interaction: discord.Interaction) -> None:
    waitlist_rows = bot.db.get_waitlist()
    embed = discord.Embed(title="Caster Request Waitlist", color=discord.Color.dark_blue())

    if not waitlist_rows:
        embed.description = "No waiting requests."
    else:
        lines = []
        for idx, row in enumerate(waitlist_rows, 1):
            lines.append(
                f"**{idx}.** <@{row['requester_id']}> • {row['event_time']} • {row['status'].capitalize()} (#{row['request_id']})"
            )
        embed.description = "\n".join(lines)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="removerequest", description="Remove a request from the queue")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(request_id="Request ID to remove", reason="Reason for removal")
async def removerequest(interaction: discord.Interaction, request_id: int, reason: str = "Removed by staff") -> None:
    req = bot.db.close_request(request_id, "REMOVED")
    if req is None:
        await interaction.response.send_message("Request not found.", ephemeral=True)
        return

    await bot.refresh_request_message(request_id)
    if interaction.guild:
        await bot.log_event(interaction.guild, "Request Removed", req, completion_status=reason)

    await interaction.response.send_message(f"Removed request #{request_id}.", ephemeral=True)


@bot.tree.command(name="assigncaster", description="Manually assign a caster to a request")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(request_id="Request ID", caster="Caster to assign")
async def assigncaster(interaction: discord.Interaction, request_id: int, caster: discord.Member) -> None:
    req = bot.db.assign_request(request_id, caster.id)
    if req is None:
        await interaction.response.send_message("Request could not be assigned (not found or not assignable).", ephemeral=True)
        return

    await bot.refresh_request_message(request_id)
    if interaction.guild:
        await bot.send_assignment_notifications(interaction.guild, req)
    await interaction.response.send_message(f"Assigned request #{request_id} to {caster.mention}.", ephemeral=True)


@bot.tree.command(name="closecastrequest", description="Close a caster request")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(request_id="Request ID", completion_status="Completed/Cancelled/No_Show/etc")
async def closecastrequest(interaction: discord.Interaction, request_id: int, completion_status: str = "COMPLETED") -> None:
    req = bot.db.close_request(request_id, completion_status)
    if req is None:
        await interaction.response.send_message("Request not found.", ephemeral=True)
        return

    await bot.refresh_request_message(request_id)

    if interaction.guild:
        await bot.log_event(interaction.guild, "Request Closed", req, completion_status=completion_status.upper())
        requester = interaction.guild.get_member(req.requester_id)
        if requester:
            try:
                await requester.send(f"Your caster request #{request_id} has been marked as {completion_status.upper()}.")
            except discord.Forbidden:
                pass
        if req.assigned_caster_id:
            caster = interaction.guild.get_member(req.assigned_caster_id)
            if caster:
                try:
                    await caster.send(
                        f"Request #{request_id} that you were assigned to has been marked as {completion_status.upper()}."
                    )
                except discord.Forbidden:
                    pass

    await interaction.response.send_message(
        f"Request #{request_id} marked as {completion_status.upper()}.", ephemeral=True
    )


@bot.tree.command(name="casterstatus", description="Post caster readiness controls and current status")
@app_commands.checks.has_permissions(manage_guild=True)
async def casterstatus(interaction: discord.Interaction) -> None:
    ready_count = bot.db.count_ready()
    wait_count = len(bot.db.get_waitlist())
    embed = discord.Embed(title="Caster Status", color=discord.Color.purple())
    embed.add_field(name="Ready Casters", value=str(ready_count), inline=True)
    embed.add_field(name="Waiting Requests", value=str(wait_count), inline=True)
    embed.set_footer(text="Casters can toggle Ready/Not Ready anytime.")
    await interaction.response.send_message(embed=embed, view=bot.ready_view)


@requestcast.error
@waitlist.error
@removerequest.error
@assigncaster.error
@closecastrequest.error
@casterstatus.error
async def command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.errors.MissingPermissions):
        if interaction.response.is_done():
            await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
        else:
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return
    if interaction.response.is_done():
        await interaction.followup.send(f"Error: {error}", ephemeral=True)
    else:
        await interaction.response.send_message(f"Error: {error}", ephemeral=True)


if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN environment variable is required.")
    bot.run(token)
