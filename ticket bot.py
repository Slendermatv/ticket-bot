"""Discord ticket system with persistent panels, ticket controls, and transcripts.

Configuration is loaded from environment variables or a .env file beside this
script. Never commit a real bot token.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands


BASE_DIR = Path(__file__).resolve().parent
TICKETS_FILE = BASE_DIR / "tickets.json"
BLACKLIST_FILE = BASE_DIR / "blacklist.json"
PANEL_EMBED_FILE = BASE_DIR / "ticket-panel.json"


def load_dotenv(path: Path) -> None:
	"""Load basic KEY=VALUE entries without overriding real environment vars."""
	if not path.is_file():
		return
	for raw_line in path.read_text(encoding="utf-8").splitlines():
		line = raw_line.strip()
		if not line or line.startswith("#"):
			continue
		if line.startswith("export "):
			line = line[7:].strip()
		key, separator, value = line.partition("=")
		if not separator:
			continue
		key = key.strip()
		value = value.strip()
		if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
			value = value[1:-1]
		if key and key not in os.environ:
			os.environ[key] = value


load_dotenv(BASE_DIR / ".env")


def env_int(name: str, default: int = 0) -> int:
	try:
		return int(os.getenv(name, str(default)).strip() or default)
	except ValueError:
		logging.warning("Ignoring invalid integer in %s", name)
		return default


def env_ids(name: str) -> list[int]:
	result: list[int] = []
	for part in os.getenv(name, "").split(","):
		try:
			value = int(part.strip())
			if value > 0 and value not in result:
				result.append(value)
		except ValueError:
			continue
	return result


TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
GUILD_ID = env_int("DISCORD_GUILD_ID")
LOG_CHANNEL_ID = env_int("TICKET_LOG_CHANNEL_ID")
ARCHIVE_CATEGORY_ID = env_int("TICKET_ARCHIVE_CATEGORY_ID")
STAFF_ROLE_IDS = env_ids("TICKET_STAFF_ROLE_IDS")
TICKET_PING_ROLE_IDS = env_ids("TICKET_PING_ROLE_IDS")
BRAND_NAME = os.getenv("TICKET_BRAND_NAME", "Support Desk")
GLOBAL_STAFF = set([*STAFF_ROLE_IDS, *TICKET_PING_ROLE_IDS])

CATEGORIES: dict[str, dict[str, str]] = {
	"report": {"name": "Report", "emoji": "📝", "description": "Report an issue, concern, or incident."},
	"solicitor": {"name": "Solicitor Request", "emoji": "⚖️", "description": "Request legal or solicitor support."},
	"executive": {"name": "Executive Branch", "emoji": "🏛️", "description": "Executive branch support."},
	"department": {"name": "Department Support", "emoji": "🏢", "description": "Help with a government department."},
	"legislative": {"name": "Legislative Support", "emoji": "📜", "description": "Legislative branch support."},
	"judicial": {"name": "Judicial Support", "emoji": "🧭", "description": "Judicial branch support."},
	"state_law": {"name": "State Law Support", "emoji": "📚", "description": "State law support."},
}

COLORS = {
	"brand": discord.Color.from_rgb(31, 55, 89),
	"success": discord.Color.green(),
	"warning": discord.Color.orange(),
	"danger": discord.Color.red(),
}
ACTIVE_STATUSES = {"open", "on_hold"}
CREATE_LOCK = asyncio.Lock()
logger = logging.getLogger("ticket_bot")


def category_channel_id(key: str) -> int:
	return env_int(f"TICKET_CATEGORY_ID_{key.upper()}")


def category_role_ids(key: str) -> list[int]:
	return env_ids(f"TICKET_ROLE_IDS_{key.upper()}")


def read_json(path: Path, fallback: Any) -> Any:
	try:
		with path.open("r", encoding="utf-8") as file:
			return json.load(file)
	except (OSError, json.JSONDecodeError):
		return fallback


def atomic_json_write(path: Path, data: Any) -> None:
	temporary = path.with_suffix(path.suffix + ".tmp")
	temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
	temporary.replace(path)


def load_ticket_data() -> dict[str, Any]:
	data = read_json(TICKETS_FILE, {})
	if isinstance(data, list):
		data = {"counter": len(data), "tickets": {str(row.get("id", i)): row for i, row in enumerate(data) if isinstance(row, dict)}}
	if not isinstance(data, dict):
		data = {}
	tickets = data.get("tickets", {})
	if not isinstance(tickets, dict):
		tickets = {}
	return {"counter": max(0, int(data.get("counter", len(tickets)) or 0)), "tickets": tickets}


def save_ticket_data() -> None:
	atomic_json_write(TICKETS_FILE, TICKET_DATA)


def load_blacklist() -> set[int]:
	data = read_json(BLACKLIST_FILE, [])
	if isinstance(data, dict):
		data = data.get("users", [])
	if not isinstance(data, list):
		return set()
	result: set[int] = set()
	for value in data:
		try:
			result.add(int(value))
		except (TypeError, ValueError):
			continue
	return result


def save_blacklist() -> None:
	atomic_json_write(BLACKLIST_FILE, sorted(BLACKLISTED_USERS))


TICKET_DATA = load_ticket_data()
TICKETS: dict[str, dict[str, Any]] = TICKET_DATA["tickets"]
BLACKLISTED_USERS = load_blacklist()


def ticket_for_channel(channel_id: int) -> dict[str, Any] | None:
	return next(
		(ticket for ticket in TICKETS.values() if int(ticket.get("channel_id", 0)) == channel_id),
		None,
	)


def is_active(ticket: dict[str, Any]) -> bool:
	return ticket.get("status") in ACTIVE_STATUSES


def is_staff(member: discord.Member) -> bool:
	if member.guild_permissions.administrator or member.guild_permissions.manage_channels:
		return True
	configured_roles = set(STAFF_ROLE_IDS)
	for key in CATEGORIES:
		configured_roles.update(category_role_ids(key))
	return any(role.id in configured_roles for role in member.roles)


def staff_roles_for(key: str, guild: discord.Guild) -> list[discord.Role]:
	role_ids = list(dict.fromkeys([*STAFF_ROLE_IDS, *TICKET_PING_ROLE_IDS, *category_role_ids(key)]))
	return [role for role_id in role_ids if (role := guild.get_role(role_id)) is not None]


def clean_channel_name(value: str) -> str:
	result = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")[:40]
	return result or "user"


def ticket_embed(title: str, description: str, color: discord.Color = COLORS["brand"]) -> discord.Embed:
	embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
	embed.set_footer(text=f"{BRAND_NAME} • Ticket System")
	return embed


def build_ticket_panel_embed() -> discord.Embed:
	"""Load a Discord embed JSON template, with a modern built-in fallback."""
	data = read_json(PANEL_EMBED_FILE, None)
	if isinstance(data, dict):
		try:
			return discord.Embed.from_dict(data)
		except (KeyError, TypeError, ValueError):
			logger.exception("Invalid ticket panel embed JSON; using the default panel")

	embed = discord.Embed(
		title=f"{BRAND_NAME} | Support Center",
		description="Select a category below to open a private support ticket. Our team will follow up in your ticket channel.",
		color=COLORS["brand"],
		timestamp=datetime.now(timezone.utc),
	)
	embed.set_author(name="Tarrant County Sheriff's Office")
	embed.add_field(
		name="Available categories",
		value="\n".join(f"{item['emoji']} **{item['name']}** — {item['description']}" for item in CATEGORIES.values()),
		inline=False,
	)
	embed.add_field(
		name="Before you open a ticket",
		value="Share the details staff need to help. Never include passwords or other sensitive account information.",
		inline=False,
	)
	embed.set_footer(text=f"{BRAND_NAME} • Choose a category below")
	return embed


async def interaction_reply(
	interaction: discord.Interaction,
	content: str,
	*,
	ephemeral: bool = True,
) -> None:
	if interaction.response.is_done():
		await interaction.followup.send(content, ephemeral=ephemeral)
	else:
		await interaction.response.send_message(content, ephemeral=ephemeral)


async def get_ticket_from_interaction(
	interaction: discord.Interaction,
	*,
	active_only: bool = True,
) -> dict[str, Any] | None:
	if interaction.channel is None:
		return None
	ticket = ticket_for_channel(interaction.channel.id)
	if not ticket or (active_only and not is_active(ticket)):
		await interaction_reply(interaction, "This isn't an active ticket channel.")
		return None
	return ticket


def ticket_access_overwrites(
	guild: discord.Guild,
	requester: discord.Member,
	key: str,
) -> dict[Any, discord.PermissionOverwrite]:
	overwrites: dict[Any, discord.PermissionOverwrite] = {
		guild.default_role: discord.PermissionOverwrite(view_channel=False),
		requester: discord.PermissionOverwrite(
			view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True
		),
	}
	if guild.me:
		overwrites[guild.me] = discord.PermissionOverwrite(
			view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, attach_files=True
		)
	for role in staff_roles_for(key, guild):
		overwrites[role] = discord.PermissionOverwrite(
			view_channel=True, send_messages=True, read_message_history=True, attach_files=True
		)
	return overwrites


async def create_ticket(
	interaction: discord.Interaction,
	key: str,
	answers: dict[str, str],
) -> None:
	guild = interaction.guild
	user = interaction.user
	if guild is None or not isinstance(user, discord.Member):
		await interaction_reply(interaction, "Tickets can only be opened inside a server.")
		return
	if user.id in BLACKLISTED_USERS:
		await interaction_reply(interaction, "You can't open tickets on this server. Contact a server administrator if you think this is a mistake.")
		return

	async with CREATE_LOCK:
		existing = next(
			(row for row in TICKETS.values() if int(row.get("guild_id", 0)) == guild.id and int(row.get("user_id", 0)) == user.id and is_active(row)),
			None,
		)
		if existing:
			existing_channel = guild.get_channel(int(existing.get("channel_id", 0)))
			where = existing_channel.mention if existing_channel else "your existing ticket"
			await interaction_reply(interaction, f"You already have an active ticket: {where}")
			return

		TICKET_DATA["counter"] += 1
		ticket_id = TICKET_DATA["counter"]
		category = CATEGORIES[key]
		channel_name = f"ticket-{ticket_id:04d}-{clean_channel_name(category['name'])}-{clean_channel_name(user.display_name)}"
		parent_id = category_channel_id(key)
		parent = guild.get_channel(parent_id) if parent_id else None
		if parent_id and not isinstance(parent, discord.CategoryChannel):
			logger.warning("Configured category channel %s is missing or not a category", parent_id)
			parent = None

		try:
			channel = await guild.create_text_channel(
				channel_name,
				category=parent,
				overwrites=ticket_access_overwrites(guild, user, key),
				topic=f"Ticket #{ticket_id} • {category['name']} • Opened by {user} ({user.id})",
				reason=f"Ticket #{ticket_id} opened by {user}",
			)
		except discord.Forbidden:
			TICKET_DATA["counter"] -= 1
			await interaction_reply(interaction, "I can't create ticket channels. Check my Manage Channels and View Channel permissions.")
			return
		except discord.HTTPException as exc:
			TICKET_DATA["counter"] -= 1
			logger.exception("Failed to create ticket channel")
			await interaction_reply(interaction, f"Couldn't create the ticket channel: {exc}")
			return

		now = datetime.now(timezone.utc).isoformat()
		ticket = {
			"id": ticket_id,
			"guild_id": guild.id,
			"channel_id": channel.id,
			"user_id": user.id,
			"category": key,
			"status": "open",
			"priority": "medium",
			"claimed_by": None,
			"created_at": now,
			"participants": [],
			"notes": [],
			"answers": answers,
		}
		TICKETS[str(ticket_id)] = ticket
		save_ticket_data()

		embed = ticket_embed(f"🎫 Ticket #{ticket_id:04d}", "Thanks for contacting the support team. Describe anything else that may help us assist you.")
		embed.add_field(name="Ticket ID", value=f"`#{ticket_id:04d}`", inline=True)
		embed.add_field(name="Category", value=f"{category['emoji']} {category['name']}", inline=True)
		embed.add_field(name="Opened by", value=user.mention, inline=True)
		embed.add_field(name="Priority", value="🟡 Medium", inline=True)
		for label, value in answers.items():
			if value:
				embed.add_field(name=label[:256], value=value[:1024], inline=False)
		controls = TicketControlView(ticket_id)
		try:
			support_roles = staff_roles_for(key, guild)
			role_mentions = " ".join(role.mention for role in support_roles)
			message = await channel.send(
				content=f"{user.mention} {role_mentions}".strip(),
				embed=embed,
				view=controls,
				allowed_mentions=discord.AllowedMentions(users=[user], roles=support_roles, everyone=False),
			)
			ticket["control_message_id"] = message.id
			save_ticket_data()
		except discord.HTTPException:
			logger.exception("Ticket channel created but initial message failed")

		await interaction_reply(interaction, f"Your ticket is ready: {channel.mention}")


async def make_transcript(channel: discord.TextChannel) -> discord.File:
	lines = [f"Transcript for #{channel.name} ({channel.id})", "=" * 72]
	async for message in channel.history(limit=None, oldest_first=True):
		timestamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
		body = message.clean_content or "[no text content]"
		if message.attachments:
			body += "\nAttachments: " + ", ".join(attachment.url for attachment in message.attachments)
		for embed in message.embeds:
			if embed.title or embed.description:
				body += f"\n[Embed] {embed.title or ''} {embed.description or ''}".strip()
		lines.append(f"[{timestamp}] {message.author} ({message.author.id}):\n{body}")
	data = "\n\n".join(lines).encode("utf-8")
	return discord.File(io.BytesIO(data), filename=f"ticket-{channel.id}-transcript.txt")


async def close_ticket(
	interaction: discord.Interaction,
	ticket: dict[str, Any],
) -> None:
	channel = interaction.channel
	guild = interaction.guild
	user = interaction.user
	if not isinstance(channel, discord.TextChannel) or guild is None:
		await interaction_reply(interaction, "The ticket channel is unavailable.")
		return
	if not isinstance(user, discord.Member):
		await interaction_reply(interaction, "Couldn't verify your server permissions.")
		return
	if user.id != int(ticket.get("user_id", 0)) and not is_staff(user):
		await interaction_reply(interaction, "Only the ticket owner or staff can close this ticket.")
		return
	if not is_active(ticket):
		await interaction_reply(interaction, "This ticket is already closed.")
		return

	if not interaction.response.is_done():
		await interaction.response.defer(ephemeral=True, thinking=True)
	ticket["status"] = "closed"
	ticket["closed_at"] = datetime.now(timezone.utc).isoformat()
	ticket["closed_by"] = user.id
	save_ticket_data()

	try:
		owner = guild.get_member(int(ticket["user_id"]))
		if owner:
			await channel.set_permissions(owner, view_channel=False, send_messages=False, reason="Ticket closed")
		archive = guild.get_channel(ARCHIVE_CATEGORY_ID) if ARCHIVE_CATEGORY_ID else None
		edit: dict[str, Any] = {"name": f"closed-{int(ticket['id']):04d}", "reason": f"Ticket closed by {user}"}
		if isinstance(archive, discord.CategoryChannel):
			edit["category"] = archive
		await channel.edit(**edit)
		control_id = ticket.get("control_message_id")
		if control_id:
			try:
				control_message = await channel.fetch_message(int(control_id))
				await control_message.edit(view=None)
			except (discord.NotFound, discord.Forbidden, discord.HTTPException):
				pass
		await channel.send(embed=ticket_embed("🔒 Ticket Closed", f"Closed by {user.mention}. The channel has been archived; staff can still review the conversation.", COLORS["danger"]))
	except discord.Forbidden:
		logger.warning("Missing permissions while archiving ticket %s", ticket.get("id"))
	except discord.HTTPException:
		logger.exception("Failed to archive ticket %s", ticket.get("id"))

	log_channel = guild.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None
	if isinstance(log_channel, discord.TextChannel):
		category_name = CATEGORIES.get(ticket.get("category"), {}).get("name", ticket.get("category"))
		log_embed = ticket_embed(f"📋 Ticket #{int(ticket['id']):04d} Closed", f"**Ticket ID:** `#{int(ticket['id']):04d}`\n**Category:** {category_name}\n**Opened by:** <@{ticket['user_id']}>\n**Closed by:** {user.mention}\n**Channel:** {channel.mention}", COLORS["warning"])
		try:
			await log_channel.send(embed=log_embed, file=await make_transcript(channel))
		except discord.HTTPException:
			logger.exception("Failed to send ticket transcript for #%s", ticket.get("id"))
	await interaction.followup.send("Ticket closed and archived.", ephemeral=True)


class TicketPanelSelect(discord.ui.Select):
	def __init__(self) -> None:
		options = [
			discord.SelectOption(label=data["name"], value=key, emoji=data["emoji"], description=data["description"][:100])
			for key, data in CATEGORIES.items()
		]
		super().__init__(
			placeholder="Choose a support category…",
			min_values=1,
			max_values=1,
			options=options,
			custom_id="tickets:choose-category",
		)

	async def callback(self, interaction: discord.Interaction) -> None:
		key = self.values[0]
		if key not in CATEGORIES:
			await interaction_reply(interaction, "That ticket category is unavailable.")
			return
		if interaction.user.id in BLACKLISTED_USERS:
			await interaction_reply(interaction, "You can't open tickets on this server.")
			return
		if any(int(row.get("guild_id", 0)) == (interaction.guild_id or 0) and int(row.get("user_id", 0)) == interaction.user.id and is_active(row) for row in TICKETS.values()):
			await interaction_reply(interaction, "You already have an active ticket. Close it before opening another.")
			return
		if key == "report":
			await interaction.response.send_modal(ReportTicketModal(key))
		else:
			await interaction.response.send_modal(GeneralTicketModal(key))


class TicketPanelView(discord.ui.View):
	def __init__(self) -> None:
		super().__init__(timeout=None)
		self.add_item(TicketPanelSelect())


class GeneralTicketModal(discord.ui.Modal):
	def __init__(self, key: str) -> None:
		self.category_key = key
		super().__init__(title=f"{CATEGORIES[key]['name']} Support", timeout=300)
		self.description = discord.ui.TextInput(
			label="How can we help?",
			placeholder="Please describe your request in detail…",
			style=discord.TextStyle.paragraph,
			max_length=1500,
		)
		self.add_item(self.description)

	async def on_submit(self, interaction: discord.Interaction) -> None:
		await create_ticket(interaction, self.category_key, {"Request": self.description.value.strip()})


class ReportTicketModal(discord.ui.Modal):
	def __init__(self, key: str) -> None:
		self.category_key = key
		super().__init__(title="Submit a Report", timeout=300)
		self.subject = discord.ui.TextInput(label="Who or what are you reporting?", max_length=150)
		self.details = discord.ui.TextInput(label="Describe the issue", style=discord.TextStyle.paragraph, max_length=1500)
		self.evidence = discord.ui.TextInput(label="Evidence or extra details (optional)", style=discord.TextStyle.paragraph, required=False, max_length=800)
		self.add_item(self.subject)
		self.add_item(self.details)
		self.add_item(self.evidence)

	async def on_submit(self, interaction: discord.Interaction) -> None:
		await create_ticket(interaction, self.category_key, {
			"Subject": self.subject.value.strip(),
			"Details": self.details.value.strip(),
			"Evidence / extra details": self.evidence.value.strip() or "Not provided",
		})


class ConfirmCloseView(discord.ui.View):
	def __init__(self, ticket_id: int) -> None:
		super().__init__(timeout=60)
		self.ticket_id = ticket_id

	@discord.ui.button(label="Confirm close", style=discord.ButtonStyle.danger, emoji="🔒")
	async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
		ticket = TICKETS.get(str(self.ticket_id))
		if not ticket:
			await interaction_reply(interaction, "Ticket not found.")
			return
		await close_ticket(interaction, ticket)

	@discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
	async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
		await interaction.response.edit_message(content="Ticket close cancelled.", view=None)


class TicketControlView(discord.ui.View):
	def __init__(self, ticket_id: int) -> None:
		super().__init__(timeout=None)
		self.ticket_id = ticket_id
		claim = discord.ui.Button(label="Claim", style=discord.ButtonStyle.primary, emoji="🙋", custom_id=f"tickets:claim:{ticket_id}")
		claim.callback = self.claim_ticket
		self.add_item(claim)
		close = discord.ui.Button(label="Close ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id=f"tickets:close:{ticket_id}")
		close.callback = self.request_close
		self.add_item(close)

	async def claim_ticket(self, interaction: discord.Interaction) -> None:
		if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
			await interaction_reply(interaction, "Only staff can claim tickets.")
			return
		ticket = TICKETS.get(str(self.ticket_id))
		if not ticket or not is_active(ticket):
			await interaction_reply(interaction, "This ticket isn't active.")
			return
		claimed_by = ticket.get("claimed_by")
		if claimed_by and int(claimed_by) != interaction.user.id:
			await interaction_reply(interaction, f"Already claimed by <@{claimed_by}>.")
			return
		ticket["claimed_by"] = interaction.user.id
		save_ticket_data()
		await interaction.response.send_message(embed=ticket_embed("🙋 Ticket Claimed", f"{interaction.user.mention} is handling this ticket.", COLORS["success"]), ephemeral=False)

	async def request_close(self, interaction: discord.Interaction) -> None:
		ticket = TICKETS.get(str(self.ticket_id))
		if not ticket or not is_active(ticket):
			await interaction_reply(interaction, "This ticket isn't active.")
			return
		if interaction.user.id != int(ticket.get("user_id", 0)) and not (isinstance(interaction.user, discord.Member) and is_staff(interaction.user)):
			await interaction_reply(interaction, "Only the ticket owner or staff can close this ticket.")
			return
		await interaction.response.send_message("Are you sure you want to close this ticket?", view=ConfirmCloseView(self.ticket_id), ephemeral=True)


class TicketBot(commands.Bot):
	async def setup_hook(self) -> None:
		self.add_view(TicketPanelView())
		for ticket in TICKETS.values():
			if is_active(ticket):
				self.add_view(TicketControlView(int(ticket["id"])))
		if GUILD_ID:
			guild = discord.Object(id=GUILD_ID)
			self.tree.copy_global_to(guild=guild)
			synced = await self.tree.sync(guild=guild)
			logger.info("Synced %s commands to guild %s", len(synced), GUILD_ID)
		else:
			synced = await self.tree.sync()
			logger.info("Synced %s global commands", len(synced))


intents = discord.Intents.default()
intents.guilds = True
bot = TicketBot(command_prefix=commands.when_mentioned, intents=intents)


def staff_only(interaction: discord.Interaction) -> bool:
	return isinstance(interaction.user, discord.Member) and is_staff(interaction.user)


async def require_staff(interaction: discord.Interaction) -> bool:
	if not staff_only(interaction):
		await interaction_reply(interaction, "This command is for ticket staff only.")
		return False
	return True


@bot.tree.command(name="ticket-panel", description="Post the support ticket panel")
async def ticket_panel(interaction: discord.Interaction) -> None:
	if not await require_staff(interaction):
		return
	embed = build_ticket_panel_embed()
	image_path = BASE_DIR.parent / "Pics" / "help-center.png"
	uses_local_image = any(
		component.url == "attachment://help-center.png"
		for component in (embed.thumbnail, embed.image)
	)
	if uses_local_image and image_path.is_file():
		file = discord.File(image_path, filename="help-center.png")
		await interaction.response.send_message(embed=embed, view=TicketPanelView(), file=file)
	else:
		await interaction.response.send_message(embed=embed, view=TicketPanelView())


@bot.tree.command(name="claim", description="Claim the ticket you're viewing")
async def claim_command(interaction: discord.Interaction) -> None:
	if not staff_only(interaction):
		await interaction_reply(interaction, "Only ticket staff can claim tickets.")
		return
	ticket = await get_ticket_from_interaction(interaction)
	if not ticket:
		return
	if ticket.get("claimed_by") and int(ticket["claimed_by"]) != interaction.user.id:
		await interaction_reply(interaction, f"Already claimed by <@{ticket['claimed_by']}>.")
		return
	ticket["claimed_by"] = interaction.user.id
	save_ticket_data()
	await interaction_reply(interaction, f"Ticket #{ticket['id']} is now claimed by {interaction.user.mention}.", ephemeral=False)


@bot.tree.command(name="close-ticket", description="Close and archive this ticket")
async def close_command(interaction: discord.Interaction) -> None:
	ticket = await get_ticket_from_interaction(interaction)
	if ticket:
		await close_ticket(interaction, ticket)


@bot.tree.command(name="add-user", description="Give a member access to this ticket")
@app_commands.describe(member="Member to add")
async def add_user(interaction: discord.Interaction, member: discord.Member) -> None:
	if not await require_staff(interaction):
		return
	ticket = await get_ticket_from_interaction(interaction)
	if not ticket or not isinstance(interaction.channel, discord.TextChannel):
		return
	await interaction.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
	participants = ticket.setdefault("participants", [])
	if member.id not in participants:
		participants.append(member.id)
	save_ticket_data()
	await interaction_reply(interaction, f"Added {member.mention} to the ticket.", ephemeral=False)


@bot.tree.command(name="remove-user", description="Remove a member's access from this ticket")
@app_commands.describe(member="Member to remove")
async def remove_user(interaction: discord.Interaction, member: discord.Member) -> None:
	if not await require_staff(interaction):
		return
	ticket = await get_ticket_from_interaction(interaction)
	if not ticket or not isinstance(interaction.channel, discord.TextChannel):
		return
	if member.id == int(ticket["user_id"]):
		await interaction_reply(interaction, "The ticket owner can't be removed from their own ticket.")
		return
	await interaction.channel.set_permissions(member, overwrite=None)
	ticket["participants"] = [int(value) for value in ticket.get("participants", []) if int(value) != member.id]
	save_ticket_data()
	await interaction_reply(interaction, f"Removed {member.mention} from the ticket.", ephemeral=False)


@bot.tree.command(name="rename-ticket", description="Rename this ticket channel")
@app_commands.describe(name="New channel name")
async def rename_ticket(interaction: discord.Interaction, name: app_commands.Range[str, 1, 90]) -> None:
	if not await require_staff(interaction):
		return
	ticket = await get_ticket_from_interaction(interaction)
	if not ticket or not isinstance(interaction.channel, discord.TextChannel):
		return
	await interaction.channel.edit(name=clean_channel_name(name), reason=f"Renamed by {interaction.user}")
	await interaction_reply(interaction, f"Ticket channel renamed to **{interaction.channel.name}**.", ephemeral=False)


@bot.tree.command(name="priority", description="Set ticket priority")
@app_commands.choices(level=[app_commands.Choice(name=value.title(), value=value) for value in ("low", "medium", "high")])
async def priority_command(interaction: discord.Interaction, level: app_commands.Choice[str]) -> None:
	if not await require_staff(interaction):
		return
	ticket = await get_ticket_from_interaction(interaction)
	if not ticket:
		return
	ticket["priority"] = level.value
	save_ticket_data()
	await interaction_reply(interaction, f"Priority set to **{level.name}**.", ephemeral=False)


@bot.tree.command(name="ticket-note", description="Add a staff-only note to the ticket")
@app_commands.describe(text="Note to add")
async def ticket_note(interaction: discord.Interaction, text: app_commands.Range[str, 1, 1000]) -> None:
	if not await require_staff(interaction):
		return
	ticket = await get_ticket_from_interaction(interaction)
	if not ticket:
		return
	note = {"author_id": interaction.user.id, "text": text, "created_at": datetime.now(timezone.utc).isoformat()}
	ticket.setdefault("notes", []).append(note)
	save_ticket_data()
	await interaction_reply(interaction, "Staff note saved (not shown to the ticket owner).")


@bot.tree.command(name="alert-staff", description="Ping ticket staff for help")
async def alert_staff(interaction: discord.Interaction) -> None:
	ticket = await get_ticket_from_interaction(interaction)
	if not ticket or not isinstance(interaction.channel, discord.TextChannel):
		return
	roles = staff_roles_for(str(ticket["category"]), interaction.guild) if interaction.guild else []
	mentions = " ".join(role.mention for role in roles)
	await interaction.channel.send(
		f"{mentions}\nStaff attention requested by {interaction.user.mention}.",
		allowed_mentions=discord.AllowedMentions(users=[interaction.user], roles=roles, everyone=False),
	)
	await interaction_reply(interaction, "Staff have been alerted.")


@bot.tree.command(name="ticket-hold", description="Put this ticket on hold")
async def ticket_hold(interaction: discord.Interaction) -> None:
	if not await require_staff(interaction):
		return
	ticket = await get_ticket_from_interaction(interaction)
	if not ticket or not isinstance(interaction.channel, discord.TextChannel) or interaction.guild is None:
		return
	owner = interaction.guild.get_member(int(ticket["user_id"]))
	if owner:
		await interaction.channel.set_permissions(owner, view_channel=True, send_messages=False, read_message_history=True)
	ticket["status"] = "on_hold"
	save_ticket_data()
	await interaction_reply(interaction, "Ticket placed on hold; the requester can still read the channel.", ephemeral=False)


@bot.tree.command(name="ticket-unhold", description="Resume a ticket on hold")
async def ticket_unhold(interaction: discord.Interaction) -> None:
	if not await require_staff(interaction):
		return
	ticket = await get_ticket_from_interaction(interaction)
	if not ticket or not isinstance(interaction.channel, discord.TextChannel) or interaction.guild is None:
		return
	owner = interaction.guild.get_member(int(ticket["user_id"]))
	if owner:
		await interaction.channel.set_permissions(owner, view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
	ticket["status"] = "open"
	save_ticket_data()
	await interaction_reply(interaction, "Ticket resumed.", ephemeral=False)


@bot.tree.command(name="move-ticket", description="Move this ticket into a configured category")
@app_commands.describe(category="Ticket category key (for example: report, judicial)")
async def move_ticket(interaction: discord.Interaction, category: str) -> None:
	if not await require_staff(interaction):
		return
	ticket = await get_ticket_from_interaction(interaction)
	key = category.strip().lower().replace("-", "_").replace(" ", "_")
	if not ticket or key not in CATEGORIES:
		if ticket:
			await interaction_reply(interaction, "Unknown category. Use one of: " + ", ".join(CATEGORIES))
		return
	if not isinstance(interaction.channel, discord.TextChannel) or interaction.guild is None:
		return
	target_id = category_channel_id(key)
	target = interaction.guild.get_channel(target_id) if target_id else None
	if target_id and not isinstance(target, discord.CategoryChannel):
		await interaction_reply(interaction, "The configured Discord category could not be found.")
		return
	old_specific_roles = set(category_role_ids(str(ticket.get("category", ""))))
	new_specific_roles = set(category_role_ids(key))
	for role_id in old_specific_roles - new_specific_roles - GLOBAL_STAFF:
		role = interaction.guild.get_role(role_id)
		if role:
			await interaction.channel.set_permissions(role, overwrite=None)
	for role in staff_roles_for(key, interaction.guild):
		await interaction.channel.set_permissions(
			role,
			view_channel=True,
			send_messages=True,
			read_message_history=True,
			attach_files=True,
		)
	await interaction.channel.edit(category=target)
	ticket["category"] = key
	save_ticket_data()
	await interaction_reply(interaction, f"Ticket moved to **{CATEGORIES[key]['name']}**.", ephemeral=False)


@bot.tree.command(name="ticket-info", description="Show details about this ticket")
async def ticket_info(interaction: discord.Interaction) -> None:
	if not await require_staff(interaction):
		return
	ticket = await get_ticket_from_interaction(interaction, active_only=False)
	if not ticket:
		return
	category = CATEGORIES.get(ticket.get("category"), {}).get("name", ticket.get("category", "Unknown"))
	claimed_by = ticket.get("claimed_by")
	claimed = f"<@{claimed_by}>" if claimed_by else "Unclaimed"
	details = (
		f"**Category:** {category}\n**Status:** {ticket.get('status')}\n"
		f"**Priority:** {ticket.get('priority', 'medium').title()}\n"
		f"**Requester:** <@{ticket['user_id']}>\n**Claimed by:** {claimed}\n"
		f"**Opened:** {ticket.get('created_at', 'Unknown')}"
	)
	embed = ticket_embed(f"Ticket #{ticket['id']}", details)
	await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="ticket-list", description="List active tickets in this server")
async def ticket_list(interaction: discord.Interaction) -> None:
	if not await require_staff(interaction):
		return
	rows = [ticket for ticket in TICKETS.values() if interaction.guild_id and int(ticket.get("guild_id", 0)) == interaction.guild_id and is_active(ticket)]
	if not rows:
		await interaction_reply(interaction, "There are no active tickets.")
		return
	rows.sort(key=lambda item: int(item.get("id", 0)))
	lines = [f"`#{row['id']}` <#{row['channel_id']}> • {CATEGORIES.get(row.get('category'), {}).get('name', row.get('category'))} • <@{row['user_id']}> • {row.get('priority', 'medium').title()}" for row in rows[:25]]
	embed = ticket_embed(f"Active Tickets ({len(rows)})", "\n".join(lines))
	await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="ticket-stats", description="Show ticket system statistics")
async def ticket_stats(interaction: discord.Interaction) -> None:
	if not await require_staff(interaction):
		return
	rows = [row for row in TICKETS.values() if interaction.guild_id and int(row.get("guild_id", 0)) == interaction.guild_id]
	opened = sum(row.get("status") == "open" for row in rows)
	on_hold = sum(row.get("status") == "on_hold" for row in rows)
	closed = sum(row.get("status") == "closed" for row in rows)
	embed = ticket_embed("📊 Ticket Statistics", f"**Total:** {len(rows)}\n**Open:** {opened}\n**On hold:** {on_hold}\n**Closed:** {closed}\n**Blacklisted users:** {len(BLACKLISTED_USERS)}")
	await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="ticket-blacklist", description="Prevent a member from opening tickets")
@app_commands.describe(member="Member to blacklist")
async def blacklist_user(interaction: discord.Interaction, member: discord.Member) -> None:
	if not await require_staff(interaction):
		return
	BLACKLISTED_USERS.add(member.id)
	save_blacklist()
	await interaction_reply(interaction, f"{member.mention} can no longer open tickets.", ephemeral=False)


@bot.tree.command(name="ticket-unblacklist", description="Allow a member to open tickets again")
@app_commands.describe(member="Member to remove from the blacklist")
async def unblacklist_user(interaction: discord.Interaction, member: discord.Member) -> None:
	if not await require_staff(interaction):
		return
	BLACKLISTED_USERS.discard(member.id)
	save_blacklist()
	await interaction_reply(interaction, f"{member.mention} can open tickets again.", ephemeral=False)


@bot.event
async def on_ready() -> None:
	logger.info("Connected as %s (guilds: %s)", bot.user, len(bot.guilds))


@bot.event
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
	logger.error("Slash command failed", exc_info=(type(error), error, error.__traceback__))
	await interaction_reply(interaction, "That command failed. Check that the bot has the required channel permissions.")


def main() -> None:
	logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
	if not TOKEN:
		raise SystemExit("Set DISCORD_BOT_TOKEN in codes/.env or your environment before starting the bot.")
	bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
	main()
