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
import json
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
        {"roster": [], "kos": [], "eoi_channel_id": None},
    )
    # make sure older save files get any new keys
    g.setdefault("roster", [])
    g.setdefault("kos", [])
    g.setdefault("eoi_channel_id", None)
    return g


# --------------------------------------------------------------------------- #
# Bot setup
# --------------------------------------------------------------------------- #

intents = discord.Intents.default()
# message content is not needed; everything here is slash commands + components
bot = commands.Bot(command_prefix="!", intents=intents)
DATA = load_data()


def is_leadership(member: discord.Member) -> bool:
    """True if the member has the leadership role or is a server admin."""
    if member.guild_permissions.administrator:
        return True
    return any(r.name == LEADERSHIP_ROLE_NAME for r in member.roles)


def now_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


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
        msg = interaction.message
        if msg is None:
            await interaction.response.send_message(
                "Could not read this message.", ephemeral=True
            )
            return

        record = self.votes.setdefault(msg.id, {})
        previous = record.get(interaction.user.id)
        record[interaction.user.id] = choice

        yes, no = self._tally(msg.id)

        # update the embed footer with the live tally
        embed = msg.embeds[0] if msg.embeds else discord.Embed()
        embed.set_footer(text=f"✅ {yes}   •   ❌ {no}")
        await msg.edit(embed=embed, view=self)

        if previous == choice:
            note = f"Your **{choice.upper()}** vote is already counted."
        elif previous:
            note = f"Changed your vote to **{choice.upper()}**."
        else:
            note = f"Counted your **{choice.upper()}** vote."
        await interaction.response.send_message(note, ephemeral=True)

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
        label="Montage (YouTube link)",
        placeholder="https://youtube.com/...",
        required=False,
        max_length=300,
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
        store = guild_store(DATA, interaction.guild_id)
        channel_id = store.get("eoi_channel_id")

        if channel_id is None:
            await interaction.response.send_message(
                "No EOI channel is set yet. Ask leadership to run "
                "`/eoi_channel set` first.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(channel_id)
        if channel is None:
            await interaction.response.send_message(
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
        embed.add_field(name="Discord", value=self.discord_name.value, inline=True)
        embed.add_field(name="Name", value=self.ign.value, inline=True)
        montage_val = self.montage.value.strip() or "—"
        embed.add_field(name="Montage", value=montage_val, inline=False)
        embed.add_field(
            name="Armoury",
            value=self.armoury.value.strip() or "—",
            inline=False,
        )
        embed.add_field(
            name="Vouches / Previous Gangs",
            value=self.extra.value.strip() or "—",
            inline=False,
        )
        embed.set_author(
            name=str(interaction.user),
            icon_url=interaction.user.display_avatar.url,
        )
        embed.set_footer(text="✅ 0   •   ❌ 0")

        await channel.send(embed=embed, view=EOI_VIEW)
        await interaction.response.send_message(
            f"Your EOI has been sent to {channel.mention}. Good luck. 🫡",
            ephemeral=True,
        )


# --------------------------------------------------------------------------- #
# Command groups
# --------------------------------------------------------------------------- #

roster_group = app_commands.Group(name="roster", description="Manage the gang roster")
kos_group = app_commands.Group(name="kos", description="Manage the Kill-On-Sight list")
eoi_channel_group = app_commands.Group(
    name="eoi_channel", description="Configure the EOI channel"
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

@roster_group.command(name="view", description="Show the current gang roster")
async def roster_view(interaction: discord.Interaction) -> None:
    store = guild_store(DATA, interaction.guild_id)
    embed = _format_list("🎯 Gang Roster", store["roster"], "The roster is empty.")
    await interaction.response.send_message(embed=embed)


@roster_group.command(name="add", description="Add a member to the roster")
@app_commands.describe(
    name="Member name / RP name",
    role="Optional rank or role note (e.g. OG, Prospect, Driver)",
)
async def roster_add(
    interaction: discord.Interaction, name: str, role: str | None = None
) -> None:
    store = guild_store(DATA, interaction.guild_id)
    if any(m["name"].lower() == name.lower() for m in store["roster"]):
        await interaction.response.send_message(
            f"**{name}** is already on the roster.", ephemeral=True
        )
        return
    store["roster"].append(
        {"name": name, "note": role, "added_by": str(interaction.user), "at": now_stamp()}
    )
    save_data(DATA)
    await interaction.response.send_message(f"Added **{name}** to the roster. ✅")


@roster_group.command(name="remove", description="Remove a member from the roster")
@app_commands.describe(name="The member name to remove")
async def roster_remove(interaction: discord.Interaction, name: str) -> None:
    store = guild_store(DATA, interaction.guild_id)
    before = len(store["roster"])
    store["roster"] = [m for m in store["roster"] if m["name"].lower() != name.lower()]
    if len(store["roster"]) == before:
        await interaction.response.send_message(
            f"Couldn't find **{name}** on the roster.", ephemeral=True
        )
        return
    save_data(DATA)
    await interaction.response.send_message(f"Removed **{name}** from the roster. 🗑️")


@roster_group.command(name="reset", description="Wipe the entire roster (leadership only)")
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
    await interaction.response.send_message(f"Roster wiped — {count} entries cleared. 🧹")


# ---- /kos ------------------------------------------------------------------- #

@kos_group.command(name="view", description="Show the Kill-On-Sight list")
async def kos_view(interaction: discord.Interaction) -> None:
    store = guild_store(DATA, interaction.guild_id)
    embed = _format_list("💀 KOS List", store["kos"], "The KOS list is empty.")
    await interaction.response.send_message(embed=embed)


@kos_group.command(name="add", description="Add a player or gang to the KOS list")
@app_commands.describe(
    name="Player or gang name",
    reason="Optional reason (e.g. shot on sight, rival gang)",
)
async def kos_add(
    interaction: discord.Interaction, name: str, reason: str | None = None
) -> None:
    store = guild_store(DATA, interaction.guild_id)
    if any(k["name"].lower() == name.lower() for k in store["kos"]):
        await interaction.response.send_message(
            f"**{name}** is already on the KOS list.", ephemeral=True
        )
        return
    store["kos"].append(
        {"name": name, "note": reason, "added_by": str(interaction.user), "at": now_stamp()}
    )
    save_data(DATA)
    await interaction.response.send_message(f"Added **{name}** to the KOS list. 💀")


@kos_group.command(name="remove", description="Remove an entry from the KOS list")
@app_commands.describe(name="The name to remove")
async def kos_remove(interaction: discord.Interaction, name: str) -> None:
    store = guild_store(DATA, interaction.guild_id)
    before = len(store["kos"])
    store["kos"] = [k for k in store["kos"] if k["name"].lower() != name.lower()]
    if len(store["kos"]) == before:
        await interaction.response.send_message(
            f"Couldn't find **{name}** on the KOS list.", ephemeral=True
        )
        return
    save_data(DATA)
    await interaction.response.send_message(f"Removed **{name}** from the KOS list. 🗑️")


@kos_group.command(name="reset", description="Wipe the KOS list (leadership only)")
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
    await interaction.response.send_message(f"KOS list wiped — {count} entries cleared. 🧹")


# ---- /eoi_channel ----------------------------------------------------------- #

@eoi_channel_group.command(name="set", description="Set the channel EOIs are sent to")
@app_commands.describe(channel="The channel applications will be posted in")
async def eoi_channel_set(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    if not is_leadership(interaction.user):
        await interaction.response.send_message(
            "Only leadership can set the EOI channel.", ephemeral=True
        )
        return
    store = guild_store(DATA, interaction.guild_id)
    store["eoi_channel_id"] = channel.id
    save_data(DATA)
    await interaction.response.send_message(
        f"EOI applications will now be sent to {channel.mention}. ✅", ephemeral=True
    )


@eoi_channel_group.command(name="show", description="Show the current EOI channel")
async def eoi_channel_show(interaction: discord.Interaction) -> None:
    store = guild_store(DATA, interaction.guild_id)
    cid = store.get("eoi_channel_id")
    if cid is None:
        await interaction.response.send_message(
            "No EOI channel is set yet.", ephemeral=True
        )
        return
    channel = interaction.guild.get_channel(cid)
    where = channel.mention if channel else "a channel that no longer exists"
    await interaction.response.send_message(
        f"EOIs are sent to {where}.", ephemeral=True
    )


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
        title="📝 Expression of Interest — Recruitment Open",
        description=message
        or "Want to roll with us? Click **Apply** below and fill out the form. "
        "Leadership will review and vote on every application.",
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
    embed = discord.Embed(title="🛠️ Gang Bot — Commands", color=EMBED_COLOR)
    embed.add_field(
        name="Roster",
        value=(
            "`/roster view` — show the roster\n"
            "`/roster add` — add a member\n"
            "`/roster remove` — remove a member\n"
            "`/roster reset` — wipe it (leadership)"
        ),
        inline=False,
    )
    embed.add_field(
        name="KOS",
        value=(
            "`/kos view` — show the KOS list\n"
            "`/kos add` — add a player/gang\n"
            "`/kos remove` — remove an entry\n"
            "`/kos reset` — wipe it (leadership)"
        ),
        inline=False,
    )
    embed.add_field(
        name="EOI / Recruitment",
        value=(
            "`/eoi` — open the application form\n"
            "`/eoi_post` — post an Apply button (leadership)\n"
            "`/eoi_channel set` — set the EOI channel (leadership)\n"
            "`/eoi_channel show` — show the EOI channel"
        ),
        inline=False,
    )
    embed.set_footer(text=f"Leadership role: {LEADERSHIP_ROLE_NAME}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

bot.tree.add_command(roster_group)
bot.tree.add_command(kos_group)
bot.tree.add_command(eoi_channel_group)


@bot.event
async def on_ready() -> None:
    # register persistent views so buttons survive restarts
    bot.add_view(EOI_VIEW)
    bot.add_view(EOIPromptView())
    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash commands.", len(synced))
    except discord.DiscordException as exc:
        log.error("Command sync failed: %s", exc)
    log.info("Logged in as %s (id: %s)", bot.user, bot.user.id)


def main() -> None:
    if not TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Add it as an environment variable."
        )
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
