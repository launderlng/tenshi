"""
FiveM Gang Management Bot
-------------------------
Slash-command bot for managing a FiveM roleplay gang's Discord:

  /roster view            - show the current gang roster
  /roster add             - add a member to the roster
  /roster remove          - remove a member from the roster
  /roster reset           - wipe the roster (leadership only)

  /kos view               - show the Kill-On-Sight list
  /kos add                - add a player/gang to the KOS list
  /kos remove             - remove an entry from the KOS list
  /kos reset              - wipe the KOS list (leadership only)

  /eoi                    - open an application form (modal). Posts the
                            filled application to the EOI channel with
                            Yes / No voting buttons that leadership can tick.

  /eoi_channel set        - set the channel EOI applications are sent to
                            (leadership only)

  /gang_help              - list every command

Data is stored in a local JSON file so everything survives a restart.
Set the bot token in the DISCORD_TOKEN environment variable.
"""

import os
import io
import re
import json
import random
import asyncio
import logging
import datetime
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

TOKEN = os.getenv("DISCORD_TOKEN")

# Name of the role that is allowed to run leadership-only actions
# (reset roster/KOS, set the EOI channel, and vote on applications).
LEADERSHIP_ROLE_NAME = os.getenv("LEADERSHIP_ROLE", "Leadership")

# Where persistent data lives. On Railway, point this at a mounted
# volume path if you want it to survive redeploys (e.g. /data/gang_data.json).
DATA_FILE = os.getenv("DATA_FILE", "gang_data.json")

# Lock the bot to specific servers. Comma-separated guild IDs in ALLOWED_GUILD_IDS;
# the bot leaves any server not on the list. Empty = no restriction.
_allowed_raw = os.getenv("ALLOWED_GUILD_IDS", "").replace(" ", "")
ALLOWED_GUILD_IDS = {int(x) for x in _allowed_raw.split(",") if x.isdigit()}

EMBED_COLOR = 0xE11D48  # deep red, fits the "gang" vibe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("gang-bot")


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

def _default_data() -> dict[str, Any]:
    return {
        # per-guild data keyed by guild id (as string)
        "guilds": {}
    }


def load_data() -> dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return _default_data()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Could not read %s (%s). Starting fresh.", DATA_FILE, exc)
        return _default_data()


def save_data(data: dict[str, Any]) -> None:
    tmp = DATA_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)  # atomic write
    except OSError as exc:
        log.error("Failed to save data: %s", exc)


def guild_store(data: dict[str, Any], guild_id: int) -> dict[str, Any]:
    """Return (creating if needed) the data bucket for one guild."""
    g = data["guilds"].setdefault(
        str(guild_id),
        {
            "roster": [],
            "kos": [],
            "eoi_channel_id": None,
            "roster_role_id": None,
            "kos_role_id": None,
            "self_roles": [],
            "radio_freq": None,
            "radio_ping_role_id": None,
            "ranks": [],
            "log_channel_id": None,
            "ticket_log_channel_id": None,
            "order_log_channel_id": None,
            "join_log_channel_id": None,
            "welcome_channel_id": None,
            "staff_channel_id": None,
            "roster_log_channel_id": None,
            "kos_log_channel_id": None,
            "radio_log_channel_id": None,
            "eoi_log_channel_id": None,
            "member_log_channel_id": None,
            "orders": [],
            "next_order_id": 1,
            "tickets_open": True,
            "ticket_category_id": None,
            "ticket_types": [],
            "leadership_role_id": None,
            "transcript_channel_id": None,
        },
    )
    # make sure older save files get any new keys
    g.setdefault("leadership_role_id", None)
    g.setdefault("transcript_channel_id", None)
    g.setdefault("ticket_types", [])
    g.setdefault("roster", [])
    g.setdefault("kos", [])
    g.setdefault("eoi_channel_id", None)
    g.setdefault("roster_role_id", None)
    g.setdefault("kos_role_id", None)
    g.setdefault("self_roles", [])
    g.setdefault("radio_freq", None)
    g.setdefault("radio_ping_role_id", None)
    g.setdefault("ranks", [])
    g.setdefault("log_channel_id", None)
    g.setdefault("ticket_log_channel_id", None)
    g.setdefault("order_log_channel_id", None)
    g.setdefault("roster_log_channel_id", None)
    g.setdefault("kos_log_channel_id", None)
    g.setdefault("radio_log_channel_id", None)
    g.setdefault("eoi_log_channel_id", None)
    g.setdefault("member_log_channel_id", None)
    g.setdefault("join_log_channel_id", None)
    g.setdefault("welcome_channel_id", None)
    g.setdefault("staff_channel_id", None)
    g.setdefault("kos_musters", {})
    g.setdefault("orders", [])
    g.setdefault("next_order_id", 1)
    g.setdefault("tickets_open", True)
    g.setdefault("ticket_category_id", None)
    return g


# --------------------------------------------------------------------------- #
# Bot setup
# --------------------------------------------------------------------------- #

intents = discord.Intents.default()
# Join/leave events + welcome messages need the members intent, which is
# privileged. Turning it on here ALSO requires enabling "Server Members Intent"
# in the Developer Portal — so it is opt-in via an env var to avoid crashing
# the whole bot when the portal toggle isn't set yet.
# To enable: flip Server Members Intent in the portal, then set MEMBERS_INTENT=true.
MEMBERS_INTENT = os.getenv("MEMBERS_INTENT", "").lower() in ("1", "true", "yes", "on")
intents.members = MEMBERS_INTENT
bot = commands.Bot(command_prefix="!", intents=intents)
DATA = load_data()


def is_leadership(member: discord.Member) -> bool:
    """True if the member is a server admin or has the configured leadership role.

    The leadership role can be set in-server with /set_leadership (stored by id);
    otherwise it falls back to a role named LEADERSHIP_ROLE_NAME.
    """
    if member.guild_permissions.administrator:
        return True
    store = guild_store(DATA, member.guild.id)
    rid = store.get("leadership_role_id")
    if rid and any(r.id == rid for r in member.roles):
        return True
    return any(r.name == LEADERSHIP_ROLE_NAME for r in member.roles)


def now_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


LOG_CATEGORIES = {
    # category -> (emoji, label, color)
    "roster": ("📋", "Roster", 0x3B82F6),
    "kos": ("💀", "KOS", 0xDC2626),
    "radio": ("📻", "Radio", 0xA855F7),
    "eoi": ("📨", "Recruitment", 0x10B981),
    "members": ("🎭", "Roles", 0xEC4899),
    "joins": ("🚪", "Joins & Leaves", 0x14B8A6),
    "tickets": ("🎫", "Tickets", 0x0EA5E9),
    "orders": ("💰", "Orders", 0xF59E0B),
    "config": ("⚙️", "Config", 0x6B7280),
    "activity": ("•", "Activity", EMBED_COLOR),
}

# which stored channel each category logs to (falls back to the general log)
LOG_ROUTING = {
    "roster": "roster_log_channel_id",
    "kos": "kos_log_channel_id",
    "radio": "radio_log_channel_id",
    "eoi": "eoi_log_channel_id",
    "members": "member_log_channel_id",
    "joins": "join_log_channel_id",
    "tickets": "ticket_log_channel_id",
    "orders": "order_log_channel_id",
}


async def send_log(
    guild: "discord.Guild",
    store: dict[str, Any],
    text: str,
    *,
    category: str = "activity",
    actor: "discord.abc.User | None" = None,
    fields: "list[tuple[str, str, bool]] | None" = None,
    channel_ctx: "discord.abc.GuildChannel | None" = None,
) -> None:
    """Post a categorized, detailed audit entry to the log channel, if one is set.

    `fields` is a list of (name, value, inline) tuples for extra detail.
    `channel_ctx` adds a 'Channel' field pointing at where the action happened.
    """
    # route to the category's own channel, else fall back to the general log
    key = LOG_ROUTING.get(category)
    cid = (store.get(key) if key else None) or store.get("log_channel_id")
    if not cid:
        return
    channel = guild.get_channel(cid)
    if channel is None:
        return
    emoji, label, color = LOG_CATEGORIES.get(category, LOG_CATEGORIES["activity"])
    embed = discord.Embed(
        description=text,
        color=color,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_author(name=f"{emoji} {label}")
    if actor is not None:
        embed.add_field(
            name="Member", value=f"{actor.mention}\n`{actor}` · `{actor.id}`", inline=True
        )
    if channel_ctx is not None:
        embed.add_field(name="Channel", value=channel_ctx.mention, inline=True)
    for name, value, inline in fields or []:
        embed.add_field(name=name, value=value or "—", inline=inline)
    if actor is not None:
        embed.set_footer(text=str(actor), icon_url=actor.display_avatar.url)
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


# Matches the video id in the common YouTube URL shapes:
#   youtube.com/watch?v=ID, youtu.be/ID, youtube.com/shorts/ID, /embed/ID, /live/ID
_YT_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)


def youtube_thumbnail(url: str | None) -> str | None:
    """Return the thumbnail image URL for a YouTube link, or None if it isn't one."""
    if not url:
        return None
    match = _YT_RE.search(url)
    if not match:
        return None
    return f"https://img.youtube.com/vi/{match.group(1)}/hqdefault.jpg"


# --------------------------------------------------------------------------- #
# EOI voting view (persistent buttons)
# --------------------------------------------------------------------------- #

class EOIView(discord.ui.View):
    """Yes / No voting buttons attached to an EOI application embed.

    Votes are stored on the embed itself (as footer counts) and tracked in
    memory per-message so one person can't vote twice. The view is persistent
    (timeout=None) with a fixed custom_id so buttons keep working after a
    restart.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)
        # message_id -> {user_id: "yes"|"no"}
        # kept on the view instance; re-registered on startup
        self.votes: dict[int, dict[int, str]] = {}

    def _tally(self, message_id: int) -> tuple[int, int]:
        record = self.votes.get(message_id, {})
        yes = sum(1 for v in record.values() if v == "yes")
        no = sum(1 for v in record.values() if v == "no")
        return yes, no

    async def _handle_vote(
        self, interaction: discord.Interaction, choice: str
    ) -> None:
        log.info(
            "Vote received: choice=%s user=%s msg=%s",
            choice,
            interaction.user,
            getattr(interaction.message, "id", None),
        )
        # Defer first (silent ack) so we can never miss Discord's 3s window,
        # then do the slower edit/followup afterwards.
        try:
            await interaction.response.defer()
        except discord.InteractionResponded:
            pass

        msg = interaction.message
        if msg is None:
            await interaction.followup.send(
                "Could not read this message.", ephemeral=True
            )
            return

        record = self.votes.setdefault(msg.id, {})
        previous = record.get(interaction.user.id)
        record[interaction.user.id] = choice
        yes, no = self._tally(msg.id)

        embed = msg.embeds[0] if msg.embeds else discord.Embed()
        embed.set_footer(text=f"✅ {yes}   •   ❌ {no}")
        await msg.edit(embed=embed, view=self)

        if previous == choice:
            note = f"Your **{choice.upper()}** vote is already counted."
        elif previous:
            note = f"Changed your vote to **{choice.upper()}**."
        else:
            note = f"Counted your **{choice.upper()}** vote."
        await interaction.followup.send(note, ephemeral=True)

        if previous != choice:
            vote_emoji = "✅" if choice == "yes" else "❌"
            await send_log(
                interaction.guild,
                guild_store(DATA, interaction.guild_id),
                f"{vote_emoji} Application vote: **{choice.upper()}**",
                category="eoi",
                actor=interaction.user,
                fields=[("Tally", f"✅ {yes}  ·  ❌ {no}", True)],
            )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        log.exception(
            "EOIView error on %s: %s", getattr(item, "custom_id", "?"), error
        )
        text = (
            "⚠️ Couldn't record that vote — I may be missing permission to edit "
            "the message or post here."
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    @discord.ui.button(
        label="Yes",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="eoi:yes",
    )
    async def yes_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._handle_vote(interaction, "yes")

    @discord.ui.button(
        label="No",
        style=discord.ButtonStyle.danger,
        emoji="❌",
        custom_id="eoi:no",
    )
    async def no_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._handle_vote(interaction, "no")


# single shared view instance so vote state is consistent
EOI_VIEW = EOIView()


# --------------------------------------------------------------------------- #
# EOI application modal (the form in your screenshot + a YouTube link)
# --------------------------------------------------------------------------- #

class EOIModal(discord.ui.Modal, title="Gang Expression of Interest"):
    discord_name = discord.ui.TextInput(
        label="Discord",
        placeholder="Your Discord username / tag",
        max_length=100,
    )
    ign = discord.ui.TextInput(
        label="Name (in-game)",
        placeholder="Your FiveM character / RP name",
        max_length=100,
    )
    montage = discord.ui.TextInput(
        label="Montage(s) — one YouTube link per line",
        placeholder="https://youtube.com/...\nhttps://youtu.be/...",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=600,
    )
    armoury = discord.ui.TextInput(
        label="Armoury",
        placeholder="Weapons / loadout you run",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )
    extra = discord.ui.TextInput(
        label="Vouches / Previous Gangs",
        placeholder="Who can vouch for you + gangs you've been in",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        store = guild_store(DATA, interaction.guild_id)
        channel_id = store.get("eoi_channel_id")

        if channel_id is None:
            await interaction.followup.send(
                "No EOI channel is set yet. Ask leadership to set one from "
                "the `/setup` settings panel (EOI channel).",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(channel_id)
        if channel is None:
            await interaction.followup.send(
                "The configured EOI channel no longer exists. "
                "Ask leadership to set it again.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📨 New Expression of Interest",
            color=EMBED_COLOR,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(name="👤 Discord", value=self.discord_name.value, inline=True)
        embed.add_field(name="🎮 Name", value=self.ign.value, inline=True)
        montage_raw = self.montage.value.strip()
        links = [ln.strip() for ln in montage_raw.splitlines() if ln.strip()]
        if links:
            montage_val = "\n".join(f"• {ln}" for ln in links)
        else:
            montage_val = "—"
        label = f"🎬 Montage{'s' if len(links) > 1 else ''}"
        embed.add_field(name=label, value=montage_val[:1024], inline=False)
        embed.add_field(
            name="🔫 Armoury",
            value=self.armoury.value.strip() or "—",
            inline=False,
        )
        embed.add_field(
            name="🤝 Vouches / Previous Gangs",
            value=self.extra.value.strip() or "—",
            inline=False,
        )
        embed.set_author(
            name=str(interaction.user),
            icon_url=interaction.user.display_avatar.url,
        )
        # Preview the first montage's YouTube thumbnail as the embed image.
        thumb = youtube_thumbnail(montage_raw or None)
        if thumb:
            embed.set_image(url=thumb)
        embed.set_footer(text="✅ 0   •   ❌ 0")

        await channel.send(embed=embed, view=EOIView())
        await interaction.followup.send(
            f"Your EOI has been sent to {channel.mention}. Good luck. 🫡",
            ephemeral=True,
        )
        await send_log(
            interaction.guild,
            guild_store(DATA, interaction.guild_id),
            "📨 New application submitted.",
            category="eoi",
            actor=interaction.user,
            fields=[
                ("Name", self.ign.value, True),
                ("Discord", self.discord_name.value, True),
            ],
        )


# --------------------------------------------------------------------------- #
# Self-serve role menu (persistent dropdown)
# --------------------------------------------------------------------------- #

class SelfRoleSelect(discord.ui.Select):
    """Dropdown that gives/removes the menu's roles based on the viewer's picks.

    Whatever a member selects from this menu becomes their set of those roles:
    selected ones are added, menu roles they leave unselected are removed. The
    authoritative list of menu roles is read live from the guild's self_roles
    config, so the handler keeps working after a restart.
    """

    def __init__(self, options: list[discord.SelectOption] | None = None) -> None:
        opts = options or [discord.SelectOption(label="—", value="0")]
        super().__init__(
            custom_id="selfrole:menu",
            placeholder="Select your roles…",
            min_values=0,
            max_values=len(opts),
            options=opts,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        store = guild_store(DATA, interaction.guild_id)
        menu_ids = [r["id"] for r in store.get("self_roles", [])]
        if not menu_ids:
            await interaction.followup.send(
                "This role menu isn't set up anymore.", ephemeral=True
            )
            return

        selected = {int(v) for v in self.values if v.isdigit()}
        member = interaction.user
        added, removed, failed = [], [], []
        for rid in menu_ids:
            role = interaction.guild.get_role(rid)
            if role is None:
                continue
            has = role in member.roles
            want = rid in selected
            try:
                if want and not has:
                    await member.add_roles(role, reason="Self-role menu")
                    added.append(role.mention)
                elif not want and has:
                    await member.remove_roles(role, reason="Self-role menu")
                    removed.append(role.mention)
            except discord.HTTPException:
                failed.append(role.mention)

        parts = []
        if added:
            parts.append("➕ " + ", ".join(added))
        if removed:
            parts.append("➖ " + ", ".join(removed))
        if failed:
            parts.append(
                "⚠️ Couldn't change " + ", ".join(failed) + " (check my role position)"
            )
        await interaction.followup.send(
            "  •  ".join(parts) if parts else "No changes.", ephemeral=True
        )
        if added or removed:
            fields = []
            if added:
                fields.append(("Added", ", ".join(added), False))
            if removed:
                fields.append(("Removed", ", ".join(removed), False))
            await send_log(
                interaction.guild,
                store,
                "🎭 Self-roles updated.",
                category="members",
                actor=interaction.user,
                fields=fields,
            )


class SelfRoleView(discord.ui.View):
    def __init__(self, options: list[discord.SelectOption] | None = None) -> None:
        super().__init__(timeout=None)
        self.add_item(SelfRoleSelect(options))


# template instance registered on startup so the dropdown survives restarts
SELF_ROLE_VIEW = SelfRoleView()


# --------------------------------------------------------------------------- #
# Command groups
# --------------------------------------------------------------------------- #

roster_group = app_commands.Group(name="roster", description="Manage the gang roster")
kos_group = app_commands.Group(name="kos", description="Manage the Kill-On-Sight list")
role_menu_group = app_commands.Group(
    name="role_menu", description="Self-serve role selection menu"
)
radio_group = app_commands.Group(
    name="radio", description="Random gang radio frequency"
)


def _format_list(title: str, entries: list[dict[str, Any]], empty: str) -> discord.Embed:
    embed = discord.Embed(title=title, color=EMBED_COLOR)
    if not entries:
        embed.description = empty
        return embed
    lines = []
    for i, e in enumerate(entries, start=1):
        name = e.get("name", "Unknown")
        note = e.get("note")
        line = f"**{i}.** {name}"
        if note:
            line += f" — {note}"
        lines.append(line)
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"{len(entries)} total")
    return embed


# ---- /roster ---------------------------------------------------------------- #

def _roster_role(guild: discord.Guild, store: dict[str, Any]) -> discord.Role | None:
    """The configured roster role for this guild, or None if unset/deleted."""
    rid = store.get("roster_role_id")
    return guild.get_role(rid) if rid else None


def _roster_embed(entries: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title="🎯  Gang Roster",
        color=EMBED_COLOR,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    if not entries:
        embed.description = "*Empty.* Add members with `/roster add`."
        return embed
    lines = []
    for i, e in enumerate(entries, start=1):
        who = f"<@{e['id']}>" if e.get("id") else e.get("name", "Unknown")
        tag = f"  ·  `{e['note']}`" if e.get("note") else ""
        lines.append(f"`{i:>2}`  {who}{tag}")
    embed.description = "\n".join(lines)
    n = len(entries)
    embed.set_footer(text=f"👥 {n} member{'s' if n != 1 else ''}")
    return embed


def _live_roster_embed(
    guild: discord.Guild, store: dict[str, Any]
) -> discord.Embed:
    """Build the roster straight from the rank roles and who currently holds them."""
    embed = discord.Embed(
        title="🎯  Gang Roster",
        color=EMBED_COLOR,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    ranks = store.get("ranks", [])
    counted: set[int] = set()
    rank_role_ids: set[int] = set()

    # resolve rank → role, drop deleted roles, order by role position (top first)
    resolved = []
    for r in ranks:
        role = guild.get_role(r["role_id"])
        if role is None:
            continue
        resolved.append((r["name"], role))
        rank_role_ids.add(role.id)
    resolved.sort(key=lambda nr: nr[1].position, reverse=True)

    for name, role in resolved:
        members = role.members
        for m in members:
            counted.add(m.id)
        if members:
            shown = members[:40]
            value = " ".join(m.mention for m in shown)
            if len(members) > len(shown):
                value += f"\n*+{len(members) - len(shown)} more*"
        else:
            value = "*nobody*"
        embed.add_field(
            name=f"{name} · {len(members)}", value=value[:1024], inline=False
        )

    # anyone with the default roster role but none of the rank roles
    base = _roster_role(guild, store)
    if base is not None:
        extras = [
            m
            for m in base.members
            if m.id not in counted
            and not any(rr.id in rank_role_ids for rr in m.roles)
        ]
        for m in extras:
            counted.add(m.id)
        if extras:
            shown = extras[:40]
            value = " ".join(m.mention for m in shown)
            if len(extras) > len(shown):
                value += f"\n*+{len(extras) - len(shown)} more*"
            embed.add_field(
                name=f"Unranked · {len(extras)}", value=value[:1024], inline=False
            )

    if not embed.fields:
        embed.description = (
            "No ranks mapped yet.\nMap a rank to a server role with "
            "`/roster rank_set <name> <role>` and everyone in that role shows up here."
        )
    embed.set_footer(text=f"👥 {len(counted)} total · tap 🔄 to refresh")
    return embed


class RosterRefreshView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Refresh",
        style=discord.ButtonStyle.secondary,
        emoji="🔄",
        custom_id="roster:refresh",
    )
    async def refresh(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        store = guild_store(DATA, interaction.guild_id)
        await interaction.response.edit_message(
            embed=_live_roster_embed(interaction.guild, store), view=self
        )


ROSTER_REFRESH_VIEW = RosterRefreshView()


def _dedupe_members(members: list[discord.Member | None]) -> list[discord.Member]:
    """Drop None entries and duplicate members, preserving order."""
    seen: set[int] = set()
    out: list[discord.Member] = []
    for m in members:
        if m is None or m.id in seen:
            continue
        seen.add(m.id)
        out.append(m)
    return out


_ID_RE = re.compile(r"(\d{17,20})")


async def _collect_members(
    guild: discord.Guild,
    first: discord.Member,
    more_text: str | None,
    limit: int = 30,
) -> list[discord.Member]:
    """A picked member plus any @mentions/IDs pasted in `more_text`, up to `limit`."""
    out: list[discord.Member] = [first]
    seen = {first.id}
    for raw in _ID_RE.findall(more_text or ""):
        mid = int(raw)
        if mid in seen:
            continue
        seen.add(mid)
        member = guild.get_member(mid)
        if member is None:
            try:
                member = await guild.fetch_member(mid)
            except (discord.NotFound, discord.HTTPException):
                member = None
        if member is not None:
            out.append(member)
        if len(out) >= limit:
            break
    return out


def _rank_role(
    guild: discord.Guild, store: dict[str, Any], rank_name: str
) -> discord.Role | None:
    """The server role mapped to a rank name, or None."""
    for r in store.get("ranks", []):
        if r["name"].lower() == rank_name.lower():
            return guild.get_role(r["role_id"])
    return None


async def rank_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest the guild's configured rank names while typing the rank option."""
    store = guild_store(DATA, interaction.guild_id)
    cur = current.lower()
    return [
        app_commands.Choice(name=r["name"], value=r["name"])
        for r in store.get("ranks", [])
        if cur in r["name"].lower()
    ][:25]


@roster_group.command(
    name="rank_set",
    description="Map a rank name to a server role (leadership only)",
)
@app_commands.describe(
    name="Rank name (e.g. OG, Prospect, Driver)",
    role="The server role this rank gives",
)
async def roster_rank_set(
    interaction: discord.Interaction, name: str, role: discord.Role
) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can set ranks.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    ranks = store["ranks"]
    name_clean = name.strip()
    for r in ranks:
        if r["name"].lower() == name_clean.lower():
            r["name"] = name_clean
            r["role_id"] = role.id
            break
    else:
        if len(ranks) >= 25:
            await interaction.response.send_message(
                "You can have at most 25 ranks.", ephemeral=True
            )
            return
        ranks.append({"name": name_clean, "role_id": role.id})
    save_data(DATA)
    await interaction.response.send_message(
        f"Rank **{name_clean}** → {role.mention}. Pick it when adding to the roster. ✅",
        ephemeral=True,
    )
    await send_log(
        interaction.guild,
        store,
        f"🎖️ Rank **{name_clean}** mapped to {role.mention}.",
        category="roster",
        actor=interaction.user,
    )


@roster_group.command(
    name="rank_remove", description="Remove a configured rank (leadership only)"
)
@app_commands.describe(name="The rank name to remove")
@app_commands.autocomplete(name=rank_autocomplete)
async def roster_rank_remove(interaction: discord.Interaction, name: str) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can remove ranks.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    before = len(store["ranks"])
    store["ranks"] = [r for r in store["ranks"] if r["name"].lower() != name.lower()]
    if len(store["ranks"]) == before:
        await interaction.response.send_message(
            f"No rank called **{name}**.", ephemeral=True
        )
        return
    save_data(DATA)
    await interaction.response.send_message(
        f"Removed rank **{name}**. 🗑️", ephemeral=True
    )
    await send_log(
        interaction.guild,
        store,
        f"🎖️ Rank **{name}** removed.",
        category="roster",
        actor=interaction.user,
    )


@roster_group.command(
    name="rank_list", description="Show the configured ranks and their roles"
)
async def roster_rank_list(interaction: discord.Interaction) -> None:
    store = guild_store(DATA, interaction.guild_id)
    ranks = store["ranks"]
    embed = discord.Embed(title="🎖️ Ranks", color=EMBED_COLOR)
    if not ranks:
        embed.description = "No ranks yet. Add some with `/roster rank_set`."
    else:
        embed.description = "\n".join(
            f"**{r['name']}** → <@&{r['role_id']}>" for r in ranks
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@roster_group.command(
    name="import_roles",
    description="Add every server role as a rank in one go (leadership only)",
)
async def roster_import_roles(interaction: discord.Interaction) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can import roles.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    ranks = store["ranks"]
    existing_ids = {r["role_id"] for r in ranks}
    existing_names = {r["name"].lower() for r in ranks}

    # highest roles first; skip @everyone and bot/integration/booster roles
    candidates = [
        role
        for role in sorted(
            interaction.guild.roles, key=lambda r: r.position, reverse=True
        )
        if not role.is_default() and not role.managed
    ]

    added, skipped = [], 0
    capped = False
    for role in candidates:
        if len(ranks) >= 25:
            capped = True
            break
        if role.id in existing_ids:
            continue
        name = role.name.strip()
        if name.lower() in existing_names:
            skipped += 1
            continue
        ranks.append({"name": name, "role_id": role.id})
        existing_ids.add(role.id)
        existing_names.add(name.lower())
        added.append(role.mention)
    save_data(DATA)

    lines = []
    if added:
        lines.append(
            f"✅ Imported **{len(added)}** role(s) as ranks: {', '.join(added)}"
        )
    else:
        lines.append("Nothing new to import — every role is already a rank.")
    if capped:
        lines.append(
            "⚠️ Hit the 25-rank cap (Discord embeds max out at 25 groups). "
            "Trim with `/roster rank_remove` if you need different ones."
        )
    lines.append("Now run `/roster view` to see everyone grouped by rank.")
    await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)
    if added:
        await send_log(
            interaction.guild,
            store,
            f"🎖️ {interaction.user.mention} imported **{len(added)}** role(s) as ranks.",
            category="roster",
            actor=interaction.user,
        )


@roster_group.command(
    name="view",
    description="Show the roster, built live from your rank roles",
)
async def roster_view(interaction: discord.Interaction) -> None:
    store = guild_store(DATA, interaction.guild_id)
    embed = _live_roster_embed(interaction.guild, store)
    await interaction.response.send_message(embed=embed, view=ROSTER_REFRESH_VIEW)


@roster_group.command(
    name="add",
    description="Add up to 30 members to the roster and give them a rank's role",
)
@app_commands.describe(
    member="Member to add (@mention them)",
    more="Paste extra @mentions or IDs here to add many at once (up to 30 total)",
    rank="Rank to assign — pick from your configured ranks (optional)",
)
@app_commands.autocomplete(rank=rank_autocomplete)
async def roster_add(
    interaction: discord.Interaction,
    member: discord.Member,
    more: str | None = None,
    rank: str | None = None,
) -> None:
    await interaction.response.defer()
    store = guild_store(DATA, interaction.guild_id)
    rank_role = _rank_role(interaction.guild, store, rank) if rank else None
    base_role = _roster_role(interaction.guild, store)
    # A picked rank's role wins; otherwise fall back to the default roster role.
    role = rank_role or base_role
    targets = await _collect_members(interaction.guild, member, more, limit=30)

    added, skipped, role_fail = [], [], []
    for m in targets:
        if any(e.get("id") == m.id for e in store["roster"]):
            skipped.append(m.mention)
            continue
        if role is not None:
            try:
                await m.add_roles(role, reason=f"Roster add by {interaction.user}")
            except (discord.Forbidden, discord.HTTPException):
                role_fail.append(m.mention)
        store["roster"].append(
            {
                "id": m.id,
                "name": m.display_name,
                "note": rank,
                "added_by": str(interaction.user),
                "at": now_stamp(),
            }
        )
        added.append(m.mention)
    save_data(DATA)

    as_rank = f" as **{rank}**" if rank else ""
    lines = []
    if added:
        lines.append(f"✅ Added **{len(added)}** to the roster{as_rank}: {', '.join(added)}")
    if skipped:
        lines.append(f"ℹ️ Already on the roster: {', '.join(skipped)}.")
    if rank and rank_role is None:
        lines.append(
            f"⚠️ No role is mapped to rank **{rank}** — set one with `/roster rank_set`."
        )
    elif role is None:
        lines.append(
            "⚠️ No role to give — set a rank with `/roster rank_set`, or set the "
            "**Roster default role** in the `/setup` settings panel."
        )
    elif role_fail:
        lines.append(
            f"⚠️ Couldn't give the {role.mention} role to {', '.join(role_fail)} "
            "— check I have **Manage Roles** and my role sits above it."
        )
    await interaction.followup.send("\n".join(lines)[:2000] or "Nothing to add.")

    if added:
        await send_log(
            interaction.guild,
            store,
            f"📋 Added **{len(added)}** to the roster{as_rank}.",
            category="roster",
            actor=interaction.user,
            fields=[
                ("Members", ", ".join(added), False),
                ("Rank", rank or "—", True),
                ("Roster size", str(len(store["roster"])), True),
            ],
        )


@roster_group.command(
    name="remove",
    description="Remove a member from the roster and take the role back",
)
@app_commands.describe(member="The member to remove (@mention them)")
async def roster_remove(
    interaction: discord.Interaction, member: discord.Member
) -> None:
    store = guild_store(DATA, interaction.guild_id)
    before = len(store["roster"])
    store["roster"] = [m for m in store["roster"] if m.get("id") != member.id]
    if len(store["roster"]) == before:
        await interaction.response.send_message(
            f"{member.mention} isn't on the roster.", ephemeral=True
        )
        return

    role = _roster_role(interaction.guild, store)
    note = ""
    if role is not None and role in member.roles:
        try:
            await member.remove_roles(role, reason=f"Roster remove by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException):
            note = f"\n⚠️ Couldn't take the {role.mention} role off them."

    save_data(DATA)
    await interaction.response.send_message(
        f"Removed {member.mention} from the roster. 🗑️{note}"
    )
    await send_log(
        interaction.guild,
        store,
        f"{interaction.user.mention} removed {member.mention} from the roster.",
        category="roster",
        actor=interaction.user,
    )


@roster_group.command(
    name="reset",
    description="Wipe the roster list (keeps everyone's role) (leadership only)",
)
async def roster_reset(interaction: discord.Interaction) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can reset the roster.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    count = len(store["roster"])
    store["roster"] = []
    save_data(DATA)
    await interaction.response.send_message(
        f"Roster wiped — {count} members cleared. Roles left untouched. 🧹"
    )
    await send_log(
        interaction.guild,
        store,
        f"{interaction.user.mention} wiped the roster ({count} cleared).",
        category="roster",
        actor=interaction.user,
    )


# ---- /kos ------------------------------------------------------------------- #

def _kos_role(guild: discord.Guild, store: dict[str, Any]) -> discord.Role | None:
    """The configured KOS role for this guild, or None if unset/deleted."""
    rid = store.get("kos_role_id")
    return guild.get_role(rid) if rid else None


def _kos_embed(entries: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title="💀  Kill On Sight",
        color=0xDC2626,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    if not entries:
        embed.description = "*Empty.* Add targets with `/kos add`."
        return embed
    lines = []
    for i, e in enumerate(entries, start=1):
        who = f"<@{e['id']}>" if e.get("id") else e.get("name", "Unknown")
        tag = f"  ·  `{e['note']}`" if e.get("note") else ""
        lines.append(f"`{i:>2}`  {who}{tag}")
    embed.description = "\n".join(lines)
    n = len(entries)
    embed.set_footer(text=f"🎯 {n} on sight")
    return embed


@kos_group.command(name="view", description="Show the Kill-On-Sight list")
async def kos_view(interaction: discord.Interaction) -> None:
    store = guild_store(DATA, interaction.guild_id)
    embed = _kos_embed(store["kos"])
    await interaction.response.send_message(embed=embed)


@kos_group.command(
    name="add",
    description="Add up to 30 members to the KOS list and give them the KOS role",
)
@app_commands.describe(
    member="Member to KOS (@mention them)",
    more="Paste extra @mentions or IDs here to KOS many at once (up to 30 total)",
    reason="Optional reason applied to everyone added",
)
async def kos_add(
    interaction: discord.Interaction,
    member: discord.Member,
    more: str | None = None,
    reason: str | None = None,
) -> None:
    await interaction.response.defer()
    store = guild_store(DATA, interaction.guild_id)
    role = _kos_role(interaction.guild, store)
    targets = await _collect_members(interaction.guild, member, more, limit=30)

    added, skipped, role_fail = [], [], []
    for m in targets:
        if any(k.get("id") == m.id for k in store["kos"]):
            skipped.append(m.mention)
            continue
        if role is not None:
            try:
                await m.add_roles(role, reason=f"KOS add by {interaction.user}")
            except (discord.Forbidden, discord.HTTPException):
                role_fail.append(m.mention)
        store["kos"].append(
            {
                "id": m.id,
                "name": m.display_name,
                "note": reason,
                "added_by": str(interaction.user),
                "at": now_stamp(),
            }
        )
        added.append(m.mention)
    save_data(DATA)

    lines = []
    if added:
        lines.append(f"💀 Added **{len(added)}** to the KOS list: {', '.join(added)}")
    if skipped:
        lines.append(f"ℹ️ Already KOS: {', '.join(skipped)}.")
    if role is None:
        lines.append(
            "⚠️ No KOS role set — set the **KOS role** in the `/setup` settings "
            "panel so they get one automatically."
        )
    elif role_fail:
        lines.append(
            f"⚠️ Couldn't give the {role.mention} role to {', '.join(role_fail)} "
            "— check I have **Manage Roles** and my role sits above it."
        )
    await interaction.followup.send("\n".join(lines)[:2000] or "Nothing to add.")

    if added:
        await send_log(
            interaction.guild,
            store,
            f"💀 Added **{len(added)}** to the KOS list.",
            category="kos",
            actor=interaction.user,
            fields=[
                ("Targets", ", ".join(added), False),
                ("Reason", reason or "—", True),
                ("KOS size", str(len(store["kos"])), True),
            ],
        )


@kos_group.command(
    name="remove",
    description="Remove a member from the KOS list and take the role back",
)
@app_commands.describe(member="The member to remove (@mention them)")
async def kos_remove(
    interaction: discord.Interaction, member: discord.Member
) -> None:
    store = guild_store(DATA, interaction.guild_id)
    before = len(store["kos"])
    store["kos"] = [k for k in store["kos"] if k.get("id") != member.id]
    if len(store["kos"]) == before:
        await interaction.response.send_message(
            f"{member.mention} isn't on the KOS list.", ephemeral=True
        )
        return

    role = _kos_role(interaction.guild, store)
    note = ""
    if role is not None and role in member.roles:
        try:
            await member.remove_roles(role, reason=f"KOS remove by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException):
            note = f"\n⚠️ Couldn't take the {role.mention} role off them."

    save_data(DATA)
    await interaction.response.send_message(
        f"Removed {member.mention} from the KOS list. 🗑️{note}"
    )
    await send_log(
        interaction.guild,
        store,
        f"{interaction.user.mention} removed {member.mention} from the KOS list.",
        category="kos",
        actor=interaction.user,
    )


@kos_group.command(
    name="reset",
    description="Wipe the KOS list (keeps everyone's role) (leadership only)",
)
async def kos_reset(interaction: discord.Interaction) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can reset the KOS list.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    count = len(store["kos"])
    store["kos"] = []
    save_data(DATA)
    await interaction.response.send_message(
        f"KOS list wiped — {count} entries cleared. Roles left untouched. 🧹"
    )
    await send_log(
        interaction.guild,
        store,
        f"{interaction.user.mention} wiped the KOS list ({count} cleared).",
        category="kos",
        actor=interaction.user,
    )


# ---- /kos callout (embed + tick button for "I can make the KOS") ------------ #


def _muster_embed(text: str, attendees: list[int]) -> discord.Embed:
    embed = discord.Embed(
        title="🎯  KOS Roster",
        description=text or "*No details given.*",
        color=EMBED_COLOR,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    if attendees:
        lst = "\n".join(f"`{i:>2}` <@{uid}>" for i, uid in enumerate(attendees, 1))
    else:
        lst = "*Nobody yet — tap the button below.*"
    embed.add_field(name=f"✅ On roster · {len(attendees)}", value=lst[:1024], inline=False)
    embed.set_footer(text="Tap ✅ to mark yourself in · tap again to drop out")
    return embed


class MusterView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="I can come",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="kos:muster",
    )
    async def toggle(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        store = guild_store(DATA, interaction.guild_id)
        musters = store["kos_musters"]
        key = str(interaction.message.id)
        rec = musters.get(key)
        if rec is None:
            desc = (
                interaction.message.embeds[0].description
                if interaction.message.embeds
                else ""
            )
            rec = {"text": desc or "", "attendees": []}
            musters[key] = rec
        uid = interaction.user.id
        if uid in rec["attendees"]:
            rec["attendees"].remove(uid)
            note = "You're marked as **out**."
        else:
            rec["attendees"].append(uid)
            note = "You're marked as **in**. ✅"
        save_data(DATA)
        await interaction.response.edit_message(
            embed=_muster_embed(rec["text"], rec["attendees"]), view=self
        )
        try:
            await interaction.followup.send(note, ephemeral=True)
        except discord.HTTPException:
            pass


MUSTER_VIEW = MusterView()


@kos_group.command(
    name="roster",
    description="Post a KOS roster with a tick button for who can come (leadership)",
)
@app_commands.describe(text="What the roster post should say")
async def kos_roster(interaction: discord.Interaction, text: str) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can post a roster.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    await interaction.response.send_message(embed=_muster_embed(text, []), view=MUSTER_VIEW)
    msg = await interaction.original_response()
    store["kos_musters"][str(msg.id)] = {"text": text, "attendees": []}
    save_data(DATA)
    await send_log(
        interaction.guild,
        store,
        f"🎯 {interaction.user.mention} posted a KOS call-out.",
        category="kos",
        actor=interaction.user,
    )


# ---- Settings panel (embed + dropdowns) ------------------------------------- #

# setting value -> (label, store key, kind)
CONFIG_SETTINGS = {
    "leadership_role": ("Leadership role", "leadership_role_id", "role"),
    "log_channel": ("General log channel", "log_channel_id", "channel"),
    "roster_log_channel": ("Roster log channel", "roster_log_channel_id", "channel"),
    "kos_log_channel": ("KOS log channel", "kos_log_channel_id", "channel"),
    "radio_log_channel": ("Radio log channel", "radio_log_channel_id", "channel"),
    "eoi_log_channel": ("Recruitment log channel", "eoi_log_channel_id", "channel"),
    "member_log_channel": ("Role log channel", "member_log_channel_id", "channel"),
    "join_log_channel": ("Join/leave log channel", "join_log_channel_id", "channel"),
    "ticket_log_channel": ("Ticket log channel", "ticket_log_channel_id", "channel"),
    "order_log_channel": ("Order log channel", "order_log_channel_id", "channel"),
    "welcome_channel": ("Welcome channel", "welcome_channel_id", "channel"),
    "staff_channel": ("Staff channel", "staff_channel_id", "channel"),
    "eoi_channel": ("EOI channel", "eoi_channel_id", "channel"),
    "transcript_channel": ("Transcript channel", "transcript_channel_id", "channel"),
    "roster_role": ("Roster default role", "roster_role_id", "role"),
    "kos_role": ("KOS role", "kos_role_id", "role"),
    "radio_ping_role": ("Radio ping role", "radio_ping_role_id", "role"),
}


# options for the picker dropdown, built from CONFIG_SETTINGS
_SETTINGS_OPTIONS = [
    discord.SelectOption(
        label=label,
        value=value,
        emoji="👑" if kind == "role" else "💬",
    )
    for value, (label, key, kind) in CONFIG_SETTINGS.items()
]


async def _apply_setting(
    interaction: discord.Interaction, value_key: str, new_id: int | None, shown: str
) -> None:
    label, key, kind = CONFIG_SETTINGS[value_key]
    store = guild_store(DATA, interaction.guild_id)
    store[key] = new_id
    save_data(DATA)
    await interaction.response.send_message(
        f"⚙️  **{label}** → {shown}", ephemeral=True
    )
    await send_log(
        interaction.guild,
        store,
        f"⚙️ Setting changed: **{label}** → {shown}",
        category="config",
        actor=interaction.user,
    )


class _ChannelSetSelect(discord.ui.ChannelSelect):
    def __init__(self, value_key: str) -> None:
        self.value_key = value_key
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="Pick a channel…",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        ch = self.values[0]
        await _apply_setting(interaction, self.value_key, ch.id, ch.mention)


class _RoleSetSelect(discord.ui.RoleSelect):
    def __init__(self, value_key: str) -> None:
        self.value_key = value_key
        super().__init__(placeholder="Pick a role…", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        role = self.values[0]
        await _apply_setting(interaction, self.value_key, role.id, role.mention)


class _ClearSettingButton(discord.ui.Button):
    def __init__(self, value_key: str) -> None:
        self.value_key = value_key
        super().__init__(label="Clear", style=discord.ButtonStyle.danger, emoji="🧹")

    async def callback(self, interaction: discord.Interaction) -> None:
        await _apply_setting(interaction, self.value_key, None, "`cleared`")


class _SettingEditView(discord.ui.View):
    def __init__(self, value_key: str) -> None:
        super().__init__(timeout=180)
        _, _, kind = CONFIG_SETTINGS[value_key]
        if kind == "channel":
            self.add_item(_ChannelSetSelect(value_key))
        else:
            self.add_item(_RoleSetSelect(value_key))
        self.add_item(_ClearSettingButton(value_key))


def _settings_embed(guild: discord.Guild, store: dict[str, Any]) -> discord.Embed:
    def ch(key: str) -> str:
        cid = store.get(key)
        c = guild.get_channel(cid) if cid else None
        return c.mention if c else "`— not set`"

    def ro(key: str) -> str:
        rid = store.get(key)
        r = guild.get_role(rid) if rid else None
        return r.mention if r else "`— not set`"

    embed = discord.Embed(
        title="⚙️  Tenshi — Settings",
        description="Pick a setting from the dropdown to change it. 🔄 refreshes this list.",
        color=EMBED_COLOR,
    )
    embed.add_field(
        name="👑 Roles",
        value=(
            f"Leadership: {ro('leadership_role_id')}\n"
            f"Roster default: {ro('roster_role_id')}\n"
            f"KOS: {ro('kos_role_id')}\n"
            f"Radio ping: {ro('radio_ping_role_id')}"
        ),
        inline=False,
    )
    embed.add_field(
        name="📝 Log channels",
        value=(
            f"General: {ch('log_channel_id')}\n"
            f"📋 Roster: {ch('roster_log_channel_id')}\n"
            f"💀 KOS: {ch('kos_log_channel_id')}\n"
            f"📻 Radio: {ch('radio_log_channel_id')}\n"
            f"📨 Recruitment: {ch('eoi_log_channel_id')}\n"
            f"🎭 Roles: {ch('member_log_channel_id')}\n"
            f"🚪 Joins/Leaves: {ch('join_log_channel_id')}\n"
            f"🎫 Tickets: {ch('ticket_log_channel_id')}\n"
            f"💰 Orders: {ch('order_log_channel_id')}"
        ),
        inline=False,
    )
    embed.add_field(
        name="📁 Channels",
        value=(
            f"👋 Welcome: {ch('welcome_channel_id')}\n"
            f"🛡️ Staff: {ch('staff_channel_id')}\n"
            f"📨 EOI: {ch('eoi_channel_id')}\n"
            f"🧾 Transcripts: {ch('transcript_channel_id')}"
        ),
        inline=False,
    )
    embed.set_footer(
        text=f"🎫 {len(store.get('ticket_types', []))} ticket types · "
        f"🎖️ {len(store.get('ranks', []))} ranks"
    )
    return embed


class SettingsPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.select(
        custom_id="settings:pick",
        placeholder="⚙️  Change a setting…",
        min_values=1,
        max_values=1,
        options=_SETTINGS_OPTIONS,
    )
    async def pick(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
        value_key = select.values[0]
        if value_key == "leadership_role":
            if not interaction.user.guild_permissions.administrator:
                await interaction.response.send_message(
                    "Only a server admin can set the leadership role.", ephemeral=True
                )
                return
        elif not is_leadership(interaction.user):
            await interaction.response.send_message(
                "Only leadership can change settings.", ephemeral=True
            )
            return
        label, key, kind = CONFIG_SETTINGS[value_key]
        word = "channel" if kind == "channel" else "role"
        await interaction.response.send_message(
            f"Pick the {word} for **{label}** (or Clear it):",
            view=_SettingEditView(value_key),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Refresh",
        style=discord.ButtonStyle.secondary,
        emoji="🔄",
        custom_id="settings:refresh",
        row=1,
    )
    async def refresh(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        store = guild_store(DATA, interaction.guild_id)
        await interaction.response.edit_message(
            embed=_settings_embed(interaction.guild, store), view=self
        )


SETTINGS_PANEL_VIEW = SettingsPanelView()


# ---- /role_menu ------------------------------------------------------------- #

@role_menu_group.command(
    name="add", description="Add a role to the self-serve menu (leadership only)"
)
@app_commands.describe(
    role="A role members can give themselves",
    description="Optional short note shown next to the role in the dropdown",
)
async def role_menu_add(
    interaction: discord.Interaction,
    role: discord.Role,
    description: str | None = None,
) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can edit the role menu.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    roles = store["self_roles"]
    if any(r["id"] == role.id for r in roles):
        await interaction.response.send_message(
            f"{role.mention} is already in the menu.", ephemeral=True
        )
        return
    if len(roles) >= 25:
        await interaction.response.send_message(
            "A dropdown can hold at most 25 roles.", ephemeral=True
        )
        return
    roles.append(
        {
            "id": role.id,
            "label": role.name,
            "description": (description.strip()[:100] if description else None),
        }
    )
    save_data(DATA)
    await interaction.response.send_message(
        f"Added {role.mention} to the role menu. Run `/role_menu post` to "
        "(re)post it so the change shows. ✅",
        ephemeral=True,
    )


@role_menu_group.command(
    name="remove", description="Remove a role from the self-serve menu (leadership only)"
)
@app_commands.describe(role="The role to take out of the menu")
async def role_menu_remove(
    interaction: discord.Interaction, role: discord.Role
) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can edit the role menu.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    before = len(store["self_roles"])
    store["self_roles"] = [r for r in store["self_roles"] if r["id"] != role.id]
    if len(store["self_roles"]) == before:
        await interaction.response.send_message(
            f"{role.mention} isn't in the menu.", ephemeral=True
        )
        return
    save_data(DATA)
    await interaction.response.send_message(
        f"Removed {role.mention}. Run `/role_menu post` to repost the menu. 🗑️",
        ephemeral=True,
    )


@role_menu_group.command(name="list", description="Show the roles in the self-serve menu")
async def role_menu_list(interaction: discord.Interaction) -> None:
    store = guild_store(DATA, interaction.guild_id)
    roles = store["self_roles"]
    embed = discord.Embed(title="🎭 Role Menu", color=EMBED_COLOR)
    if not roles:
        embed.description = "No roles yet. Add some with `/role_menu add`."
    else:
        embed.description = "\n".join(
            f"- <@&{r['id']}>" + (f" — {r['description']}" if r.get("description") else "")
            for r in roles
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@role_menu_group.command(
    name="post",
    description="Post the self-serve role menu in this channel (leadership only)",
)
@app_commands.describe(
    title="Optional heading for the menu",
    description="Optional text shown above the dropdown",
)
async def role_menu_post(
    interaction: discord.Interaction,
    title: str | None = None,
    description: str | None = None,
) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can post the role menu.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    options: list[discord.SelectOption] = []
    for r in store["self_roles"]:
        role = interaction.guild.get_role(r["id"])
        if role is None:
            continue
        options.append(
            discord.SelectOption(
                label=role.name,
                value=str(role.id),
                description=r.get("description") or None,
            )
        )
    if not options:
        await interaction.response.send_message(
            "No usable roles in the menu yet — add some with `/role_menu add` first.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title=title or "🎭 Select Your Roles",
        description=description
        or "Pick the roles you want from the menu below. Anything you leave "
        "unselected will be removed.",
        color=EMBED_COLOR,
    )
    await interaction.channel.send(embed=embed, view=SelfRoleView(options))
    await interaction.response.send_message("Role menu posted. ✅", ephemeral=True)


# ---- /radio ----------------------------------------------------------------- #

def _roll_frequency() -> str:
    """A random radio frequency in 00.00 format, e.g. '47.23'."""
    return f"{random.randint(0, 99):02d}.{random.randint(0, 99):02d}"


def _radio_embed(freq: str, rolled_by: str | None = None) -> discord.Embed:
    embed = discord.Embed(
        title="📻  Gang Radio",
        description=f"**Frequency**\n# `{freq}`",
        color=EMBED_COLOR,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    if rolled_by:
        embed.set_footer(text=f"Rolled by {rolled_by}")
    return embed


def _radio_panel_embed(freq: str | None, rolled_by: str | None = None) -> discord.Embed:
    embed = discord.Embed(title="📻  Gang Radio", color=EMBED_COLOR)
    if freq:
        embed.description = (
            f"**Current frequency**\n# `{freq}`\n\n"
            "Press **📻 New Radio** to roll a fresh one."
        )
    else:
        embed.description = (
            "No frequency set yet.\nPress **📻 New Radio** below to roll one."
        )
    if rolled_by:
        embed.set_footer(text=f"Last rolled by {rolled_by}")
    return embed


class RadioPanelView(discord.ui.View):
    """Persistent panel: a button that rolls a new frequency and pings a role."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="New Radio",
        style=discord.ButtonStyle.primary,
        emoji="📻",
        custom_id="radio:new",
    )
    async def new_radio(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not is_leadership(interaction.user):
            await interaction.response.send_message(
                "Only leadership can roll a new radio.", ephemeral=True
            )
            return
        await interaction.response.defer()

        store = guild_store(DATA, interaction.guild_id)
        freq = _roll_frequency()
        store["radio_freq"] = freq
        save_data(DATA)

        # refresh the panel to show the new frequency
        try:
            await interaction.message.edit(
                embed=_radio_panel_embed(freq, str(interaction.user)), view=self
            )
        except discord.HTTPException:
            pass

        # ping the configured role with the new frequency
        rid = store.get("radio_ping_role_id")
        role = interaction.guild.get_role(rid) if rid else None
        if role is not None:
            await interaction.channel.send(
                f"{role.mention} 📻 New radio frequency: `{freq}`",
                allowed_mentions=discord.AllowedMentions(roles=True),
            )

        await interaction.followup.send(f"Rolled `{freq}`. 📻", ephemeral=True)
        await send_log(
            interaction.guild,
            store,
            "📻 New radio frequency rolled.",
            category="radio",
            actor=interaction.user,
            fields=[("Frequency", f"`{freq}`", True)],
        )


RADIO_PANEL_VIEW = RadioPanelView()


@radio_group.command(
    name="new",
    description="Roll a new random radio frequency and post it here (leadership only)",
)
async def radio_new(interaction: discord.Interaction) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can roll a new radio.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    freq = _roll_frequency()
    store["radio_freq"] = freq
    save_data(DATA)
    await interaction.channel.send(
        embed=_radio_embed(freq, rolled_by=str(interaction.user))
    )
    await interaction.response.send_message(
        f"New radio rolled: `{freq}` 📻", ephemeral=True
    )
    await send_log(
        interaction.guild,
        store,
        "📻 New radio frequency rolled.",
        category="radio",
        actor=interaction.user,
        fields=[("Frequency", f"`{freq}`", True)],
    )


@radio_group.command(name="view", description="Show the current radio frequency")
async def radio_view(interaction: discord.Interaction) -> None:
    store = guild_store(DATA, interaction.guild_id)
    freq = store.get("radio_freq")
    if not freq:
        await interaction.response.send_message(
            "No radio set yet — leadership can roll one with `/radio new`.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(embed=_radio_embed(freq))


@radio_group.command(
    name="panel",
    description="Post a radio panel with a New Radio button (leadership only)",
)
async def radio_panel(interaction: discord.Interaction) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can post the radio panel.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    await interaction.channel.send(
        embed=_radio_panel_embed(store.get("radio_freq")), view=RadioPanelView()
    )
    await interaction.response.send_message("Radio panel posted. 📻", ephemeral=True)


# ---- /eoi (open the form) --------------------------------------------------- #

@bot.tree.command(name="eoi", description="Apply to the gang — fill out an EOI form")
async def eoi(interaction: discord.Interaction) -> None:
    await interaction.response.send_modal(EOIModal())


# ---- /eoi_post (leadership force-posts a blank EOI prompt) ------------------- #

@bot.tree.command(
    name="eoi_post",
    description="Leadership: post an EOI prompt button for people to apply",
)
@app_commands.describe(
    message="Optional custom text shown above the Apply button",
)
async def eoi_post(
    interaction: discord.Interaction, message: str | None = None
) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can post the EOI prompt.", ephemeral=True
        )
        return

    store = guild_store(DATA, interaction.guild_id)
    if store.get("eoi_channel_id") is None:
        await interaction.response.send_message(
            "Set an EOI channel first with `/eoi_channel set`.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="📝  Recruitment is Open",
        description=message
        or "Want to roll with us? Tap **📨 Apply** below and fill out the form — "
        "leadership reviews and votes on every application.",
        color=EMBED_COLOR,
    )
    await interaction.channel.send(embed=embed, view=EOIPromptView())
    await interaction.response.send_message("EOI prompt posted. ✅", ephemeral=True)


class EOIPromptView(discord.ui.View):
    """A persistent 'Apply' button that opens the EOI modal for anyone."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Apply",
        style=discord.ButtonStyle.primary,
        emoji="📨",
        custom_id="eoi:apply",
    )
    async def apply(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(EOIModal())


# ---- /gang_help ------------------------------------------------------------- #

@bot.tree.command(name="gang_help", description="List every gang-bot command")
async def gang_help(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="🐉  Tenshi — Command Guide",
        description=(
            "**New here? Just run `/setup` — it builds everything and opens a "
            "settings panel with dropdowns.**"
        ),
        color=EMBED_COLOR,
    )
    embed.add_field(
        name="🎯  Roster",
        value=(
            "`/roster view` — live from your rank roles, with 🔄 refresh\n"
            "`/roster import_roles` — add every server role as a rank ⭐\n"
            "`/roster rank_set <name> <role>` — map one rank to a role\n"
            "`/roster rank_list` · `rank_remove`\n"
            "`/roster add` · `remove` · `reset` — manual list"
        ),
        inline=False,
    )
    embed.add_field(
        name="💀  KOS",
        value=(
            "`/kos view` · `/kos add @members [reason]`\n"
            "`/kos remove @member` · `/kos reset`\n"
            "`/kos roster <text>` — post a roster with a ✅ sign-up button"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎫  Tickets / Orders",
        value=(
            "`/ticket type_add` — add a type + its category\n"
            "`/ticket set_category` — where tickets open\n"
            "`/ticket panel` — post the order menu\n"
            "`/ticket board` — active orders by type\n"
            "`/ticket toggle` — open/close new orders\n"
            "`/close` — close the ticket you're in\n"
            "In a ticket: 💰 Mark Paid · 🔒 Close (saves transcript)"
        ),
        inline=False,
    )
    embed.add_field(
        name="📨  Recruitment",
        value="`/eoi` — apply · `/eoi_post` — post Apply button",
        inline=False,
    )
    embed.add_field(
        name="📻  Radio",
        value="`/radio new` · `/radio view` · `/radio panel`",
        inline=False,
    )
    embed.add_field(
        name="🎭  Role Menu",
        value="`/role_menu add` · `remove` · `list` · `post`",
        inline=False,
    )
    embed.add_field(
        name="⚙️  Setup",
        value=(
            "`/setup` — builds everything **and** opens the settings panel ⭐\n"
            "Change any channel or role right from the dropdown in that panel\n"
            "(no more typing commands for settings)"
        ),
        inline=False,
    )
    embed.set_footer(text="Staff commands need the leadership role or admin")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------- #
# Ticket / order system (multiple types: gang orders, vendors, gun crafting…)
# --------------------------------------------------------------------------- #

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:80] or "ticket"


def _new_order_id(store: dict[str, Any]) -> int:
    oid = store.get("next_order_id", 1)
    store["next_order_id"] = oid + 1
    return oid


def _find_order_by_channel(store: dict[str, Any], channel_id: int) -> dict | None:
    for o in store["orders"]:
        if o.get("channel_id") == channel_id:
            return o
    return None


def _find_ticket_type(store: dict[str, Any], name: str) -> dict | None:
    for t in store.get("ticket_types", []):
        if t["name"].lower() == name.lower():
            return t
    return None


def _order_embed(order: dict[str, Any]) -> discord.Embed:
    paid = order.get("paid", False)
    closed = order.get("status", "open") == "closed"
    color = 0x22C55E if paid else EMBED_COLOR
    ttype = order.get("type") or "Order"
    status = "🔒 Closed" if closed else ("🟢 Paid" if paid else "🔴 Unpaid")

    claimed = order.get("claimed_by")
    created = order.get("created_ts")
    lines = [
        f"**Customer**  <@{order['user_id']}>",
        f"**Status**  {status}",
        f"**Handled by**  {f'<@{claimed}>' if claimed else '*unclaimed*'}",
    ]
    if created:
        lines.append(f"**Opened**  <t:{created}:R>")

    embed = discord.Embed(
        title=f"{ttype}  ·  Order #{order['id']}",
        description="\n".join(lines),
        color=color,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(name="Items", value=order.get("guns") or "—", inline=False)
    embed.add_field(name="Quantity", value=order.get("quantity") or "—", inline=True)
    if order.get("notes"):
        embed.add_field(name="Notes", value=order["notes"], inline=False)
    return embed


def _orders_board_embed(store: dict[str, Any]) -> discord.Embed:
    active = [o for o in store["orders"] if o.get("status", "open") == "open"]
    emoji_by_type = {
        t["name"]: (t.get("emoji") or "🎫") for t in store.get("ticket_types", [])
    }

    def fmt(o: dict[str, Any]) -> str:
        items = (o.get("guns") or "—").replace("\n", " ")
        qty = (o.get("quantity") or "—").replace("\n", " ")
        mark = "🟢" if o.get("paid") else "🔴"
        line = f"{mark} `#{o['id']}` <@{o['user_id']}> — {items} ×{qty}"
        return line[:200]

    embed = discord.Embed(
        title="🎫  Active Orders",
        color=EMBED_COLOR,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    if not active:
        embed.description = "✨ No active orders right now."
        return embed

    total_paid = sum(1 for o in active if o.get("paid"))
    embed.description = (
        f"**{len(active)}** open · 🟢 **{total_paid}** paid · "
        f"🔴 **{len(active) - total_paid}** unpaid"
    )

    types: dict[str, list[dict[str, Any]]] = {}
    for o in active:
        types.setdefault(o.get("type") or "Order", []).append(o)
    for ttype, orders in types.items():
        paid = sum(1 for o in orders if o.get("paid"))
        val = "\n".join(fmt(o) for o in orders) or "None"
        if len(val) > 1024:
            val = val[:1010] + "\n…"
        em = emoji_by_type.get(ttype, "🎫")
        embed.add_field(
            name=f"{em}  {ttype}  ·  {paid}/{len(orders)} paid",
            value=val,
            inline=False,
        )
    embed.set_footer(text="💰 Mark Paid · 🔒 Close — in each ticket")
    return embed


async def _close_ticket(
    interaction: discord.Interaction, store: dict[str, Any], order: dict | None
) -> None:
    """Save a transcript (if a channel is set), log it, then delete the channel."""
    guild = interaction.guild
    channel = interaction.channel

    oid = order["id"] if order else channel.name
    ttype = (order.get("type") if order else "Ticket") or "Ticket"
    transcript = await _build_transcript(channel)
    fname = f"ticket-{oid}.txt"

    def _summary_embed() -> discord.Embed:
        e = discord.Embed(
            title=f"🧾  Transcript · {ttype} #{oid}",
            color=EMBED_COLOR,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        if order:
            e.add_field(name="Customer", value=f"<@{order['user_id']}>", inline=True)
            e.add_field(
                name="Paid",
                value="🟢 Yes" if order.get("paid") else "🔴 No",
                inline=True,
            )
            e.add_field(name="Items", value=order.get("guns") or "—", inline=False)
        e.set_footer(text=f"Closed by {interaction.user}")
        return e

    # transcript goes to: the transcript channel if set, else the ticket log,
    # else the general log
    dest_id = (
        store.get("transcript_channel_id")
        or store.get("ticket_log_channel_id")
        or store.get("log_channel_id")
    )
    dest = guild.get_channel(dest_id) if dest_id else None
    if dest is not None:
        try:
            await dest.send(
                embed=_summary_embed(),
                file=discord.File(io.BytesIO(transcript.encode("utf-8")), filename=fname),
            )
        except discord.HTTPException:
            pass

    # DM the customer a copy of their closed ticket
    if order is not None:
        member = guild.get_member(order["user_id"])
        if member is None:
            try:
                member = await guild.fetch_member(order["user_id"])
            except (discord.NotFound, discord.HTTPException):
                member = None
        if member is not None:
            dm = discord.Embed(
                title=f"🔒  Your {ttype} ticket was closed",
                description=f"Ticket **#{oid}** in **{guild.name}** has been closed.",
                color=EMBED_COLOR,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            if order.get("guns"):
                dm.add_field(name="Items", value=order["guns"], inline=False)
            dm.add_field(
                name="Paid",
                value="🟢 Yes" if order.get("paid") else "🔴 No",
                inline=True,
            )
            dm.set_footer(text="A transcript is attached.")
            try:
                await member.send(
                    embed=dm,
                    file=discord.File(
                        io.BytesIO(transcript.encode("utf-8")), filename=fname
                    ),
                )
            except discord.HTTPException:
                pass  # customer has DMs closed — nothing we can do

    # DM the staff member who closed it a copy too
    try:
        staff_dm = discord.Embed(
            title=f"🔒  You closed {ttype} #{oid}",
            description=(
                f"Ticket **#{oid}** in **{guild.name}** is closed."
                + (
                    f"\n**Customer:** <@{order['user_id']}>"
                    f"\n**Paid:** {'🟢 Yes' if order.get('paid') else '🔴 No'}"
                    if order
                    else ""
                )
            ),
            color=EMBED_COLOR,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        staff_dm.set_footer(text="Transcript attached.")
        await interaction.user.send(
            embed=staff_dm,
            file=discord.File(io.BytesIO(transcript.encode("utf-8")), filename=fname),
        )
    except discord.HTTPException:
        pass  # closer has DMs closed

    if order is not None:
        order["status"] = "closed"
        save_data(DATA)
        await send_log(
            guild,
            store,
            f"🔒 **{order.get('type', 'order')}** #{order['id']} closed.",
            category="tickets",
            actor=interaction.user,
            fields=[
                ("Order", f"#{order['id']}", True),
                ("Customer", f"<@{order['user_id']}>", True),
                ("Paid", "🟢 Yes" if order.get("paid") else "🔴 No", True),
                ("Items", order.get("guns") or "—", False),
            ],
        )

    closing = discord.Embed(
        description="🔒 **Ticket closed.** Saving transcript and deleting this "
        "channel in 5 seconds…",
        color=EMBED_COLOR,
    )
    try:
        await channel.send(embed=closing)
    except discord.HTTPException:
        pass
    await asyncio.sleep(5)
    try:
        await channel.delete(reason=f"Ticket closed by {interaction.user}")
    except discord.HTTPException:
        pass


async def _build_transcript(channel: discord.TextChannel) -> str:
    lines: list[str] = []
    try:
        async for m in channel.history(limit=None, oldest_first=True):
            ts = m.created_at.strftime("%Y-%m-%d %H:%M UTC")
            content = m.content or ""
            for e in m.embeds:
                parts = []
                if e.title:
                    parts.append(e.title)
                if e.description:
                    parts.append(e.description)
                for f in e.fields:
                    parts.append(f"{f.name}: {f.value}")
                if parts:
                    content += (" " if content else "") + "[embed] " + " | ".join(parts)
            for a in m.attachments:
                content += f" [attachment: {a.filename} {a.url}]"
            lines.append(f"[{ts}] {m.author}: {content}")
    except discord.HTTPException:
        lines.append("(couldn't read full history)")
    return "\n".join(lines) if lines else "(no messages)"


class OrderModal(discord.ui.Modal):
    def __init__(self, ticket_type: str) -> None:
        super().__init__(title=f"{ticket_type} Order"[:45])
        self.ticket_type = ticket_type
        self.guns = discord.ui.TextInput(
            label="What do you want?",
            placeholder="e.g. AK-47, Combat Pistol, parts…",
            style=discord.TextStyle.paragraph,
            max_length=500,
        )
        self.quantity = discord.ui.TextInput(
            label="How many? (per item)",
            placeholder="e.g. AK x2, Pistol x5",
            max_length=200,
        )
        self.notes = discord.ui.TextInput(
            label="Notes (optional)",
            placeholder="Delivery spot, timing, budget…",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
        )
        self.add_item(self.guns)
        self.add_item(self.quantity)
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Ack immediately — creating the channel is an API call that can take
        # longer than Discord's 3s window, which would otherwise 404 the modal.
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        me = guild.me
        if not me.guild_permissions.manage_channels:
            await interaction.followup.send(
                "I can't open a ticket — I need the **Manage Channels** permission. "
                "Tell staff.",
                ephemeral=True,
            )
            return

        store = guild_store(DATA, interaction.guild_id)
        ttype = _find_ticket_type(store, self.ticket_type)
        oid = _new_order_id(store)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True
            ),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        lead = discord.utils.get(guild.roles, name=LEADERSHIP_ROLE_NAME)
        if lead is not None:
            overwrites[lead] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True
            )

        # place the ticket under: the type's own category → the default ticket
        # category → the panel channel's category
        category = None
        if ttype and ttype.get("category_id"):
            category = guild.get_channel(ttype["category_id"])
        if category is None and store.get("ticket_category_id"):
            category = guild.get_channel(store["ticket_category_id"])
        if category is None and interaction.channel is not None:
            category = interaction.channel.category

        try:
            channel = await guild.create_text_channel(
                f"{_slug(self.ticket_type)}-{oid}",
                overwrites=overwrites,
                category=category,
                reason=f"{self.ticket_type} order by {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I couldn't open a ticket channel — check my **Manage Channels** "
                "permission and role position.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.followup.send(
                "Something went wrong opening the ticket.", ephemeral=True
            )
            return

        order = {
            "id": oid,
            "type": self.ticket_type,
            "user_id": interaction.user.id,
            "guns": self.guns.value.strip(),
            "quantity": self.quantity.value.strip(),
            "notes": self.notes.value.strip(),
            "paid": False,
            "status": "open",
            "claimed_by": None,
            "channel_id": channel.id,
            "created_at": now_stamp(),
            "created_ts": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
        }
        store["orders"].append(order)
        save_data(DATA)

        ping = f"<@{interaction.user.id}>" + (f" {lead.mention}" if lead else "")
        welcome = discord.Embed(
            description=(
                f"👋 Thanks for your **{self.ticket_type}** order, "
                f"<@{interaction.user.id}>!\n\n"
                "Staff will be with you shortly. Add anything else you need below — "
                "prices, timing, drop spot. A staff member will mark it paid once "
                "sorted, then close the ticket (you'll get a transcript in your DMs)."
            ),
            color=EMBED_COLOR,
        )
        msg = await channel.send(
            content=ping,
            embeds=[welcome, _order_embed(order)],
            view=OrderTicketView(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=True),
        )
        order["message_id"] = msg.id
        save_data(DATA)

        await interaction.followup.send(
            f"Your {self.ticket_type} ticket is open: {channel.mention} ✅",
            ephemeral=True,
        )
        await send_log(
            guild,
            store,
            f"🆕 New **{self.ticket_type}** ticket opened.",
            category="tickets",
            actor=interaction.user,
            channel_ctx=channel,
            fields=[
                ("Order", f"#{oid}", True),
                ("Type", self.ticket_type, True),
                ("Items", order["guns"] or "—", False),
                ("Quantity", order["quantity"] or "—", True),
            ],
        )


class TicketPanelSelect(discord.ui.Select):
    """Dropdown of configured ticket types; opens the order modal for the pick."""

    def __init__(self, options: list[discord.SelectOption] | None = None) -> None:
        opts = options or [discord.SelectOption(label="—", value="0")]
        super().__init__(
            custom_id="ticket:open",
            placeholder="Open a ticket…",
            min_values=1,
            max_values=1,
            options=opts,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        store = guild_store(DATA, interaction.guild_id)
        if not store.get("tickets_open", True):
            await interaction.response.send_message(
                "🔒 Ticket orders are currently **closed**. Check back later.",
                ephemeral=True,
            )
            return
        chosen = self.values[0]
        if _find_ticket_type(store, chosen) is None:
            await interaction.response.send_message(
                "That ticket type no longer exists.", ephemeral=True
            )
            return
        await interaction.response.send_modal(OrderModal(chosen))


class TicketPanelView(discord.ui.View):
    def __init__(self, options: list[discord.SelectOption] | None = None) -> None:
        super().__init__(timeout=None)
        self.add_item(TicketPanelSelect(options))


class OrderTicketView(discord.ui.View):
    """Staff buttons on a ticket: Claim, Mark Paid and Close."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Claim",
        style=discord.ButtonStyle.secondary,
        emoji="🙋",
        custom_id="order:claim",
    )
    async def claim(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not is_leadership(interaction.user):
            await interaction.response.send_message(
                "Only staff can claim tickets.", ephemeral=True
            )
            return
        await interaction.response.defer()
        store = guild_store(DATA, interaction.guild_id)
        order = _find_order_by_channel(store, interaction.channel_id)
        if order is None:
            await interaction.followup.send(
                "Couldn't find this order.", ephemeral=True
            )
            return
        prev = order.get("claimed_by")
        order["claimed_by"] = interaction.user.id
        save_data(DATA)
        await interaction.message.edit(embed=_order_embed(order), view=self)
        verb = "reassigned to" if prev and prev != interaction.user.id else "claimed by"
        await interaction.channel.send(f"🙋 Ticket {verb} {interaction.user.mention}.")
        await interaction.followup.send(
            "You're now handling this ticket. 🙋", ephemeral=True
        )
        await send_log(
            interaction.guild,
            store,
            f"🙋 **{order.get('type', 'order')}** #{order['id']} claimed.",
            category="tickets",
            actor=interaction.user,
            channel_ctx=interaction.channel,
            fields=[
                ("Order", f"#{order['id']}", True),
                ("Customer", f"<@{order['user_id']}>", True),
            ],
        )

    @discord.ui.button(
        label="Mark Paid",
        style=discord.ButtonStyle.success,
        emoji="💰",
        custom_id="order:paid",
    )
    async def mark_paid(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not is_leadership(interaction.user):
            await interaction.response.send_message(
                "Only staff can mark orders paid.", ephemeral=True
            )
            return
        await interaction.response.defer()
        store = guild_store(DATA, interaction.guild_id)
        order = _find_order_by_channel(store, interaction.channel_id)
        if order is None:
            await interaction.followup.send(
                "Couldn't find this order.", ephemeral=True
            )
            return
        order["paid"] = True
        save_data(DATA)
        await interaction.message.edit(embed=_order_embed(order), view=self)
        await interaction.followup.send(
            f"Order #{order['id']} marked **paid**. 💰", ephemeral=True
        )
        await send_log(
            interaction.guild,
            store,
            f"💰 **{order.get('type', 'order')}** #{order['id']} marked **PAID**.",
            category="orders",
            actor=interaction.user,
            channel_ctx=interaction.channel,
            fields=[
                ("Order", f"#{order['id']}", True),
                ("Customer", f"<@{order['user_id']}>", True),
                ("Items", order.get("guns") or "—", False),
            ],
        )

    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="order:close",
    )
    async def close_order(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not is_leadership(interaction.user):
            await interaction.response.send_message(
                "Only staff can close orders.", ephemeral=True
            )
            return
        await interaction.response.defer()
        store = guild_store(DATA, interaction.guild_id)
        order = _find_order_by_channel(store, interaction.channel_id)
        await interaction.followup.send(
            "Closing — transcript saved, channel will delete shortly. 🔒",
            ephemeral=True,
        )
        await _close_ticket(interaction, store, order)


TICKET_PANEL_VIEW = TicketPanelView()
ORDER_TICKET_VIEW = OrderTicketView()

ticket_group = app_commands.Group(
    name="ticket", description="Gun order / vendor / crafting ticket system"
)


async def ticket_type_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    store = guild_store(DATA, interaction.guild_id)
    cur = current.lower()
    return [
        app_commands.Choice(name=t["name"], value=t["name"])
        for t in store.get("ticket_types", [])
        if cur in t["name"].lower()
    ][:25]


@ticket_group.command(
    name="type_add",
    description="Add a ticket type and the category its tickets open under (leadership)",
)
@app_commands.describe(
    name="Type name, e.g. 'Gang Orders', 'Vendors', 'Gun Crafting'",
    category="Channel category the tickets of this type should open under",
    emoji="Optional emoji shown in the dropdown",
)
async def ticket_type_add(
    interaction: discord.Interaction,
    name: str,
    category: discord.CategoryChannel | None = None,
    emoji: str | None = None,
) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can add ticket types.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    types = store["ticket_types"]
    name_clean = name.strip()
    entry = {
        "name": name_clean,
        "category_id": category.id if category else None,
        "emoji": (emoji.strip() if emoji else None),
    }
    for t in types:
        if t["name"].lower() == name_clean.lower():
            t.update(entry)
            break
    else:
        if len(types) >= 25:
            await interaction.response.send_message(
                "You can have at most 25 ticket types.", ephemeral=True
            )
            return
        types.append(entry)
    save_data(DATA)
    where = f" under {category.mention}" if category else ""
    await interaction.response.send_message(
        f"Ticket type **{name_clean}** saved{where}. Run `/ticket panel` to "
        "(re)post the menu. ✅",
        ephemeral=True,
    )


@ticket_group.command(
    name="type_remove", description="Remove a ticket type (leadership only)"
)
@app_commands.describe(name="The ticket type to remove")
@app_commands.autocomplete(name=ticket_type_autocomplete)
async def ticket_type_remove(interaction: discord.Interaction, name: str) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can remove ticket types.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    before = len(store["ticket_types"])
    store["ticket_types"] = [
        t for t in store["ticket_types"] if t["name"].lower() != name.lower()
    ]
    if len(store["ticket_types"]) == before:
        await interaction.response.send_message(
            f"No ticket type called **{name}**.", ephemeral=True
        )
        return
    save_data(DATA)
    await interaction.response.send_message(
        f"Removed ticket type **{name}**. Repost with `/ticket panel`. 🗑️",
        ephemeral=True,
    )
    await send_log(
        interaction.guild,
        store,
        f"🎫 Ticket type **{name}** removed.",
        category="tickets",
        actor=interaction.user,
    )


@ticket_group.command(
    name="type_clear",
    description="Remove ALL ticket types at once (leadership only)",
)
async def ticket_type_clear(interaction: discord.Interaction) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can clear ticket types.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    count = len(store["ticket_types"])
    if count == 0:
        await interaction.response.send_message(
            "There are no ticket types to remove.", ephemeral=True
        )
        return
    store["ticket_types"] = []
    save_data(DATA)
    await interaction.response.send_message(
        f"Cleared **{count}** ticket type{'s' if count != 1 else ''}. "
        "Repost the menu with `/ticket panel`. 🗑️",
        ephemeral=True,
    )
    await send_log(
        interaction.guild,
        store,
        f"🎫 All ticket types cleared ({count}).",
        category="tickets",
        actor=interaction.user,
    )


@ticket_group.command(name="type_list", description="Show the configured ticket types")
async def ticket_type_list(interaction: discord.Interaction) -> None:
    store = guild_store(DATA, interaction.guild_id)
    types = store["ticket_types"]
    embed = discord.Embed(title="🎫 Ticket Types", color=EMBED_COLOR)
    if not types:
        embed.description = "No ticket types yet. Add some with `/ticket type_add`."
    else:
        lines = []
        for t in types:
            cat = f" → <#{t['category_id']}>" if t.get("category_id") else ""
            emoji = f"{t['emoji']} " if t.get("emoji") else ""
            lines.append(f"{emoji}**{t['name']}**{cat}")
        embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@ticket_group.command(
    name="set_category",
    description="Choose the category new tickets open under (leadership)",
)
@app_commands.describe(
    category="Channel category tickets open under (leave empty to clear)"
)
async def ticket_set_category(
    interaction: discord.Interaction,
    category: discord.CategoryChannel | None = None,
) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can set the ticket category.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    store["ticket_category_id"] = category.id if category else None
    save_data(DATA)
    where = (
        category.mention
        if category
        else "`cleared` — tickets open under the panel channel's category"
    )
    await interaction.response.send_message(
        f"New tickets will now open under {where}. ✅\n"
        "(A ticket **type** with its own category still overrides this.)",
        ephemeral=True,
    )


@ticket_group.command(
    name="panel",
    description="Post the ticket menu (one dropdown with all types) (leadership)",
)
@app_commands.describe(message="Optional text shown above the menu")
async def ticket_panel(
    interaction: discord.Interaction, message: str | None = None
) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can post the ticket panel.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    options: list[discord.SelectOption] = []
    menu_lines: list[str] = []
    for t in store["ticket_types"]:
        em = t.get("emoji") or "🎫"
        options.append(
            discord.SelectOption(
                label=t["name"][:100],
                value=t["name"],
                emoji=t.get("emoji") or None,
            )
        )
        menu_lines.append(f"{em}  **{t['name']}**")
    if not options:
        await interaction.response.send_message(
            "Add at least one ticket type first with `/ticket type_add`.",
            ephemeral=True,
        )
        return
    embed = discord.Embed(
        title="🎫  Open a Ticket",
        description=message
        or "Need hardware or a job done? Pick an option from the menu below and a "
        "**private ticket** opens just for you and staff.",
        color=EMBED_COLOR,
    )
    embed.add_field(name="What we handle", value="\n".join(menu_lines), inline=False)
    embed.add_field(
        name="How it works",
        value=(
            "1️⃣  Pick a category below\n"
            "2️⃣  Tell us what you want & how many\n"
            "3️⃣  A private channel opens — staff take it from there"
        ),
        inline=False,
    )
    embed.set_footer(text="Tickets are private · one click to open")
    await interaction.channel.send(embed=embed, view=TicketPanelView(options))
    await interaction.response.send_message("Ticket panel posted. ✅", ephemeral=True)


@ticket_group.command(
    name="toggle",
    description="Open or close new ticket orders for everyone (leadership)",
)
async def ticket_toggle(interaction: discord.Interaction) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can open/close ticket orders.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    now_open = not store.get("tickets_open", True)
    store["tickets_open"] = now_open
    save_data(DATA)
    state = "🟢 **open** — people can order again" if now_open else "🔒 **closed** — no new orders"
    await interaction.response.send_message(f"Ticket orders are now {state}.", ephemeral=True)
    await send_log(
        interaction.guild,
        store,
        f"🎫 Ticket orders turned **{'OPEN' if now_open else 'CLOSED'}**.",
        category="tickets",
        actor=interaction.user,
    )


@ticket_group.command(
    name="board",
    description="Show the board of active orders, grouped by type (staff)",
)
async def ticket_board(interaction: discord.Interaction) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only staff can view the orders board.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    await interaction.response.send_message(embed=_orders_board_embed(store))


@bot.tree.command(
    name="close", description="Close the ticket you're in (staff)"
)
async def close_ticket_cmd(interaction: discord.Interaction) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only staff can close tickets.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    order = _find_order_by_channel(store, interaction.channel_id)
    if order is None:
        await interaction.response.send_message(
            "This isn't a ticket channel.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        "Closing — transcript saved, channel will delete shortly. 🔒", ephemeral=True
    )
    await _close_ticket(interaction, store, order)


@bot.tree.command(
    name="setup",
    description="One-command setup — builds every channel, role and category and wires it all (leadership)",
)
async def setup_cmd(interaction: discord.Interaction) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can run setup.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    me = guild.me
    store = guild_store(DATA, interaction.guild_id)

    lead = None
    if store.get("leadership_role_id"):
        lead = guild.get_role(store["leadership_role_id"])
    if lead is None:
        lead = discord.utils.get(guild.roles, name=LEADERSHIP_ROLE_NAME)

    made_roles: list[str] = []
    made_channels: list[str] = []

    # ---- roles (KOS + Roster), only if not already set ---------------------- #
    if me.guild_permissions.manage_roles:
        role_defs = [
            ("KOS", "kos_role_id", discord.Colour(0xE11D48)),
            ("Roster", "roster_role_id", discord.Colour(0x5865F2)),
        ]
        for rname, key, colour in role_defs:
            if not store.get(key):
                try:
                    r = await guild.create_role(
                        name=rname,
                        colour=colour,
                        hoist=True,
                        mentionable=True,
                        reason="Tenshi setup",
                    )
                    store[key] = r.id
                    made_roles.append(r.mention)
                except discord.HTTPException:
                    pass

    # ---- channels + categories --------------------------------------------- #
    if me.guild_permissions.manage_channels:
        staff_ow = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if lead is not None:
            staff_ow[lead] = discord.PermissionOverwrite(view_channel=True)

        # staff-only log channels, grouped under one category
        log_defs = [
            ("gang-logs", "log_channel_id"),
            ("roster-logs", "roster_log_channel_id"),
            ("kos-logs", "kos_log_channel_id"),
            ("radio-logs", "radio_log_channel_id"),
            ("recruitment-logs", "eoi_log_channel_id"),
            ("role-logs", "member_log_channel_id"),
            ("join-logs", "join_log_channel_id"),
            ("ticket-logs", "ticket_log_channel_id"),
            ("order-logs", "order_log_channel_id"),
        ]
        needed = [(n, k) for n, k in log_defs if not store.get(k)]
        if needed:
            try:
                log_cat = await guild.create_category(
                    "📁 Tenshi Logs", overwrites=staff_ow, reason="Tenshi setup"
                )
                for name, key in needed:
                    ch = await guild.create_text_channel(
                        name, category=log_cat, overwrites=staff_ow, reason="Tenshi setup"
                    )
                    store[key] = ch.id
                made_channels.append(f"{len(needed)} log channels")
            except discord.HTTPException:
                pass

        # community channels — public ones, plus staff-only ones
        community = [
            ("welcome", "welcome_channel_id", None),
            ("applications", "eoi_channel_id", None),
            ("transcripts", "transcript_channel_id", staff_ow),
            ("staff-chat", "staff_channel_id", staff_ow),
        ]
        for name, key, ow in community:
            if not store.get(key):
                try:
                    ch = await guild.create_text_channel(
                        name, overwrites=ow, reason="Tenshi setup"
                    )
                    store[key] = ch.id
                    made_channels.append(ch.mention)
                except discord.HTTPException:
                    pass

        # Tickets category
        if not store.get("ticket_category_id"):
            try:
                tcat = await guild.create_category("🎫 Tickets", reason="Tenshi setup")
                store["ticket_category_id"] = tcat.id
                made_channels.append("🎫 Tickets category")
            except discord.HTTPException:
                pass

    save_data(DATA)

    embed = discord.Embed(
        title="🛠️  Tenshi — Setup Complete",
        description="Everything's built and wired up. Just **two** things left for you.",
        color=EMBED_COLOR,
    )
    if made_channels:
        embed.add_field(
            name="✨ Channels created", value=", ".join(made_channels), inline=False
        )
    if made_roles:
        embed.add_field(
            name="✨ Roles created", value=", ".join(made_roles), inline=False
        )
    if not made_channels and not made_roles:
        embed.add_field(
            name="✅ Already set up",
            value="Nothing new to create — everything was already configured.",
            inline=False,
        )
    embed.add_field(
        name="1️⃣  Drag my role up",
        value=(
            "In **Server Settings → Roles**, drag **Tenshi** *above* the "
            "**KOS** and **Roster** roles so I can hand them out."
        ),
        inline=False,
    )
    embed.add_field(
        name="2️⃣  Post your panels",
        value=(
            "`/eoi_post` — Apply button · `/radio panel` — radio reroll\n"
            "`/role_menu add` then `/role_menu post` — self-serve roles\n"
            "`/ticket type_add` then `/ticket panel` — order tickets"
        ),
        inline=False,
    )

    warnings = []
    if lead is None:
        warnings.append(
            "No **leadership role** found — admins already have access; set one from "
            "the dropdown below (Leadership role) to let non-admins run staff commands."
        )
    if not me.guild_permissions.manage_channels:
        warnings.append("Give me **Manage Channels** so I can create channels.")
    if not me.guild_permissions.manage_roles:
        warnings.append("Give me **Manage Roles** so I can create + hand out roles.")
    if warnings:
        embed.add_field(
            name="⚠️ Heads up", value="\n".join(f"• {w}" for w in warnings), inline=False
        )
    embed.set_footer(text="Use the dropdown below to change any setting")
    await interaction.followup.send(
        embeds=[embed, _settings_embed(guild, store)],
        view=SETTINGS_PANEL_VIEW,
        ephemeral=True,
    )


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

bot.tree.add_command(roster_group)
bot.tree.add_command(kos_group)
bot.tree.add_command(role_menu_group)
bot.tree.add_command(radio_group)
bot.tree.add_command(ticket_group)


@bot.event
async def on_interaction(interaction: discord.Interaction) -> None:
    # Diagnostic: log every interaction the bot receives so we can see whether
    # component (button) clicks are arriving and with what custom_id.
    try:
        data = interaction.data or {}
        log.info(
            "on_interaction: type=%s component_type=%s custom_id=%s user=%s",
            getattr(interaction.type, "name", interaction.type),
            data.get("component_type"),
            data.get("custom_id"),
            interaction.user,
        )
    except Exception:  # never let logging break interaction handling
        log.exception("on_interaction logging failed")


@bot.event
async def on_ready() -> None:
    # register persistent views so buttons/menus survive restarts
    bot.add_view(EOI_VIEW)
    bot.add_view(EOIPromptView())
    bot.add_view(SELF_ROLE_VIEW)
    bot.add_view(RADIO_PANEL_VIEW)
    bot.add_view(TICKET_PANEL_VIEW)
    bot.add_view(ORDER_TICKET_VIEW)
    bot.add_view(MUSTER_VIEW)
    bot.add_view(ROSTER_REFRESH_VIEW)
    bot.add_view(SETTINGS_PANEL_VIEW)
    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash commands.", len(synced))
    except discord.DiscordException as exc:
        log.error("Command sync failed: %s", exc)
    log.info("Logged in as %s (id: %s)", bot.user, bot.user.id)
    log.info(
        "In %d guild(s): %s",
        len(bot.guilds),
        ", ".join(f"{g.name} ({g.id})" for g in bot.guilds) or "none",
    )
    # enforce the server allowlist on startup, if one is configured
    if ALLOWED_GUILD_IDS:
        for g in list(bot.guilds):
            if g.id not in ALLOWED_GUILD_IDS:
                log.warning("Leaving unauthorized guild %s (%s)", g.name, g.id)
                try:
                    await g.leave()
                except discord.HTTPException:
                    pass


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    # if an allowlist is set, immediately leave any server not on it
    if ALLOWED_GUILD_IDS and guild.id not in ALLOWED_GUILD_IDS:
        log.warning("Leaving unauthorized guild %s (%s)", guild.name, guild.id)
        try:
            await guild.leave()
        except discord.HTTPException:
            pass


@bot.event
async def on_member_join(member: discord.Member) -> None:
    guild = member.guild
    store = guild_store(DATA, guild.id)

    # public welcome message
    wid = store.get("welcome_channel_id")
    wchannel = guild.get_channel(wid) if wid else None
    if wchannel is not None:
        embed = discord.Embed(
            title=f"👋  Welcome to {guild.name}!",
            description=(
                f"Hey {member.mention}, glad to have you.\n"
                "Grab your roles, read the rules, and if you want to join up use "
                "`/eoi` to apply."
            ),
            color=EMBED_COLOR,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Member #{guild.member_count}")
        try:
            await wchannel.send(
                content=member.mention,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException:
            pass

    # join log
    created = int(member.created_at.timestamp())
    await send_log(
        guild,
        store,
        f"📥 {member.mention} **joined**.",
        category="joins",
        fields=[
            ("User", f"`{member}` · `{member.id}`", True),
            ("Account made", f"<t:{created}:R>", True),
            ("Member #", str(guild.member_count), True),
        ],
    )


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    guild = member.guild
    store = guild_store(DATA, guild.id)
    joined = member.joined_at
    roles = [r.mention for r in member.roles if r.name != "@everyone"]
    fields = [
        ("User", f"`{member}` · `{member.id}`", True),
        ("Member #", str(guild.member_count), True),
    ]
    if joined is not None:
        fields.append(("Joined", f"<t:{int(joined.timestamp())}:R>", True))
    if roles:
        fields.append(("Roles", " ".join(roles)[:1024], False))
    await send_log(
        guild,
        store,
        f"📤 **{member}** left.",
        category="joins",
        fields=fields,
    )


def main() -> None:
    if not TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Add it as an environment variable."
        )
    try:
        bot.run(TOKEN, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        log.error(
            "Server Members Intent is NOT enabled. Turn it on in the Discord "
            "Developer Portal → your app → Bot → Privileged Gateway Intents → "
            "Server Members Intent, then redeploy."
        )
        raise


if __name__ == "__main__":
    main()
