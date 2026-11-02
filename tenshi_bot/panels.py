"""/role_menu, /radio, /eoi, /eoi_post and /gang_help."""

from .core import *  # noqa: F401,F403
from .roster import *  # noqa: F401,F403
from .kos import *  # noqa: F401,F403
from .settings import *  # noqa: F401,F403


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
    _brand(embed, interaction.guild)
    await _send_card(interaction.channel, [embed], lambda: SelfRoleView(options))
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
            await _edit_card(
                interaction.message,
                [_brand(_radio_panel_embed(freq, str(interaction.user)), interaction.guild)],
                RadioPanelView,
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
    await _send_card(
        interaction.channel,
        [_brand(_radio_panel_embed(store.get("radio_freq")), interaction.guild)],
        RadioPanelView,
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
    _brand(embed, interaction.guild)
    await _send_card(interaction.channel, [embed], EOIPromptView)
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
            "**New here? Use `/settings` to point me at your channels & roles "
            "— then post your panels.**"
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
            "`/kos remove @member` · `/kos reset` — full reset (keeps channels)\n"
            "`/kos start` — sign-up (pings a role) → 🎲 Draw → pick members → approve → posts to announcements + roster"
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
            "`/close [reason]` — close the ticket you're in\n"
            "`/ticket add` · `/ticket remove` — who's in a ticket\n"
            "`/ticket stats` — leaderboard · `/ticket autoclose <hours>`\n"
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
        name="🐉  Gang",
        value=(
            "`/gang dashboard` — whole gang at a glance · `/gang profile [@member]`\n"
            "`/strike add` · `list` · `remove` · `clear` · `limit`\n"
            "`/bank deposit` · `withdraw` · `balance`\n"
            "`/war start` · `result` · `end` · `view`"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️  Settings",
        value=(
            "`/settings` — view all settings, or change one ⭐\n"
            "`/settings setting:<name> channel:<#chan>` (or `role:<@role>`)\n"
            "Leave channel/role empty to clear it"
        ),
        inline=False,
    )
    if interaction.client.user:
        embed.set_thumbnail(url=interaction.client.user.display_avatar.url)
    _brand(embed, interaction.guild, "Staff commands need the leadership role or admin")
    await _send_v2(interaction, embed, ephemeral=True)


# export everything (incl. _helpers) to the modules that build on this one
__all__ = [n for n in list(globals()) if not n.startswith("__")]
