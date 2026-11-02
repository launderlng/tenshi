"""/settings — channel & role configuration."""

from .core import *  # noqa: F401,F403
from .roster import *  # noqa: F401,F403
from .kos import *  # noqa: F401,F403


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
    "eoi_ping_role": ("EOI ping role", "eoi_ping_role_id", "role"),
}


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
        description="Change any of these with `/settings setting:… channel:…` (or `role:…`).",
        color=EMBED_COLOR,
    )
    embed.add_field(
        name="👑 Roles",
        value=(
            f"Leadership: {ro('leadership_role_id')}\n"
            f"Roster default: {ro('roster_role_id')}\n"
            f"KOS: {ro('kos_role_id')}\n"
            f"Radio ping: {ro('radio_ping_role_id')}\n"
            f"EOI ping: {ro('eoi_ping_role_id')}"
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
    _brand(
        embed,
        guild,
        f"🎫 {len(store.get('ticket_types', []))} ticket types · "
        f"🎖️ {len(store.get('ranks', []))} ranks",
    )
    return embed


# export everything (incl. _helpers) to the modules that build on this one
__all__ = [n for n in list(globals()) if not n.startswith("__")]
