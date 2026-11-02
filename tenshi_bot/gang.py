"""/gang dashboard & profile, /strike, /bank, /war."""

from .core import *  # noqa: F401,F403
from .roster import *  # noqa: F401,F403
from .kos import *  # noqa: F401,F403
from .settings import *  # noqa: F401,F403
from .panels import *  # noqa: F401,F403
from .tickets import *  # noqa: F401,F403


# --------------------------------------------------------------------------- #
# Gang features: strikes · bank · wars · dashboard · member profile
# --------------------------------------------------------------------------- #

strike_group = app_commands.Group(name="strike", description="Warnings / strikes system")
bank_group = app_commands.Group(name="bank", description="Gang bank ledger")
war_group = app_commands.Group(name="war", description="Gang war tracker")
gang_group = app_commands.Group(name="gang", description="Gang dashboard & member profiles")


def _money(n: int) -> str:
    return f"${n:,}"


async def _deny(interaction: discord.Interaction, what: str = "do that") -> bool:
    if is_leadership(interaction.user):
        return False
    await interaction.response.send_message(f"Only leadership can {what}.", ephemeral=True)
    return True


# ---- strikes ---------------------------------------------------------------- #

@strike_group.command(name="add", description="Give a member a strike (leadership)")
@app_commands.describe(member="Who gets the strike", reason="Why")
async def strike_add(interaction: discord.Interaction, member: discord.Member, reason: str) -> None:
    if await _deny(interaction, "give strikes"):
        return
    store = guild_store(DATA, interaction.guild_id)
    sid = store.get("next_strike_id", 1)
    store["next_strike_id"] = sid + 1
    store["strikes"].append(
        {
            "id": sid,
            "user_id": member.id,
            "reason": reason[:300],
            "by": interaction.user.id,
            "ts": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
        }
    )
    save_data(DATA)
    count = sum(1 for s in store["strikes"] if s["user_id"] == member.id)
    limit = store.get("strike_limit", 3)
    bar = "🟥" * min(count, limit) + "⬛" * max(0, limit - count)
    embed = discord.Embed(
        title="⚠️  Strike issued",
        description=f"{member.mention} now has **{count}/{limit}** strikes\n{bar}",
        color=0xF59E0B if count < limit else 0xDC2626,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(name="Reason", value=reason[:1024], inline=False)
    embed.add_field(name="Strike ID", value=f"`#{sid}`", inline=True)
    embed.add_field(name="Issued by", value=interaction.user.mention, inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    if count >= limit:
        embed.add_field(
            name="🚨 Limit reached",
            value=f"{member.mention} has hit the **{limit}-strike** limit — leadership should review.",
            inline=False,
        )
    _brand(embed, interaction.guild)
    await interaction.response.send_message(embed=embed)
    try:
        dm = discord.Embed(
            title=f"⚠️  You received a strike in {interaction.guild.name}",
            description=f"You're on **{count}/{limit}** strikes.\n**Reason:** {reason[:1000]}",
            color=0xF59E0B,
        )
        await member.send(embed=dm)
    except discord.HTTPException:
        pass
    await send_log(
        interaction.guild, store,
        f"⚠️ Strike `#{sid}` → {member.mention} ({count}/{limit}).",
        category="roster", actor=interaction.user, fields=[("Reason", reason[:1024], False)],
    )


@strike_group.command(name="list", description="Show strikes for a member, or everyone")
@app_commands.describe(member="Leave empty to see everyone with strikes")
async def strike_list(interaction: discord.Interaction, member: discord.Member | None = None) -> None:
    store = guild_store(DATA, interaction.guild_id)
    limit = store.get("strike_limit", 3)
    embed = discord.Embed(color=0xF59E0B, timestamp=datetime.datetime.now(datetime.timezone.utc))
    if member is not None:
        mine = [s for s in store["strikes"] if s["user_id"] == member.id]
        embed.title = f"⚠️  Strikes · {member.display_name} ({len(mine)}/{limit})"
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.description = (
            "\n".join(
                f"`#{s['id']}` <t:{s['ts']}:d> — {s['reason']} *(by <@{s['by']}>)*" for s in mine
            )[:4000]
            or "✨ Clean record."
        )
    else:
        counts: dict[int, int] = {}
        for s in store["strikes"]:
            counts[s["user_id"]] = counts.get(s["user_id"], 0) + 1
        embed.title = "⚠️  Strike board"
        ranked = sorted(counts.items(), key=lambda kv: -kv[1])
        embed.description = (
            "\n".join(
                f"{'🚨' if n >= limit else '⚠️'} <@{uid}> — **{n}/{limit}**" for uid, n in ranked
            )[:4000]
            or "✨ Nobody has strikes."
        )
    _brand(embed, interaction.guild, f"Limit: {limit} strikes")
    await _send_v2(interaction, embed, ephemeral=True)


@strike_group.command(name="remove", description="Remove a strike by its ID (leadership)")
@app_commands.describe(strike_id="The # shown in /strike list")
async def strike_remove(interaction: discord.Interaction, strike_id: int) -> None:
    if await _deny(interaction, "remove strikes"):
        return
    store = guild_store(DATA, interaction.guild_id)
    hit = next((s for s in store["strikes"] if s["id"] == strike_id), None)
    if hit is None:
        await interaction.response.send_message(f"No strike `#{strike_id}`.", ephemeral=True)
        return
    store["strikes"].remove(hit)
    save_data(DATA)
    await interaction.response.send_message(f"🗑️ Removed strike `#{strike_id}` from <@{hit['user_id']}>.")
    await send_log(interaction.guild, store, f"🗑️ Strike `#{strike_id}` removed from <@{hit['user_id']}>.",
                   category="roster", actor=interaction.user)


@strike_group.command(name="clear", description="Clear all of a member's strikes (leadership)")
async def strike_clear(interaction: discord.Interaction, member: discord.Member) -> None:
    if await _deny(interaction, "clear strikes"):
        return
    store = guild_store(DATA, interaction.guild_id)
    before = len(store["strikes"])
    store["strikes"] = [s for s in store["strikes"] if s["user_id"] != member.id]
    n = before - len(store["strikes"])
    save_data(DATA)
    await interaction.response.send_message(f"🧹 Cleared **{n}** strike(s) from {member.mention}.")
    await send_log(interaction.guild, store, f"🧹 Cleared {n} strike(s) from {member.mention}.",
                   category="roster", actor=interaction.user)


@strike_group.command(name="limit", description="Set how many strikes is the limit (leadership)")
async def strike_limit(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 20]) -> None:
    if await _deny(interaction, "change the strike limit"):
        return
    store = guild_store(DATA, interaction.guild_id)
    store["strike_limit"] = limit
    save_data(DATA)
    await interaction.response.send_message(f"⚠️ Strike limit set to **{limit}**.", ephemeral=True)


# ---- bank ------------------------------------------------------------------- #

def _bank_tx(store, kind: str, amount: int, note: str, user_id: int) -> dict:
    bank = store["bank"]
    bank["balance"] = bank.get("balance", 0) + (amount if kind == "in" else -amount)
    tx = {
        "kind": kind,
        "amount": amount,
        "note": note[:200],
        "by": user_id,
        "ts": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
        "balance": bank["balance"],
    }
    bank.setdefault("tx", []).append(tx)
    bank["tx"] = bank["tx"][-500:]
    save_data(DATA)
    return tx


async def _bank_move(interaction: discord.Interaction, kind: str, amount: int, note: str) -> None:
    if await _deny(interaction, "move gang money"):
        return
    store = guild_store(DATA, interaction.guild_id)
    if kind == "out" and amount > store["bank"].get("balance", 0):
        await interaction.response.send_message(
            f"Not enough in the bank — balance is **{_money(store['bank'].get('balance', 0))}**.",
            ephemeral=True,
        )
        return
    tx = _bank_tx(store, kind, amount, note, interaction.user.id)
    arrow, color, verb = ("📥", 0x22C55E, "Deposit") if kind == "in" else ("📤", 0xDC2626, "Withdrawal")
    embed = discord.Embed(
        title=f"{arrow}  {verb} · {_money(amount)}",
        description=f"**New balance:** {_money(tx['balance'])}",
        color=color,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(name="Note", value=note or "—", inline=False)
    embed.add_field(name="By", value=interaction.user.mention, inline=True)
    _brand(embed, interaction.guild)
    await interaction.response.send_message(embed=embed)
    await send_log(
        interaction.guild, store, f"{arrow} Bank {verb.lower()} **{_money(amount)}** → {_money(tx['balance'])}.",
        category="orders", actor=interaction.user, fields=[("Note", note or "—", False)],
    )


@bank_group.command(name="deposit", description="Put money into the gang bank (leadership)")
@app_commands.describe(amount="How much", note="What it's from")
async def bank_deposit(
    interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1_000_000_000], note: str = ""
) -> None:
    await _bank_move(interaction, "in", amount, note)


@bank_group.command(name="withdraw", description="Take money out of the gang bank (leadership)")
@app_commands.describe(amount="How much", note="What it's for")
async def bank_withdraw(
    interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1_000_000_000], note: str = ""
) -> None:
    await _bank_move(interaction, "out", amount, note)


@bank_group.command(name="balance", description="Show the gang bank balance, trend + recent activity")
async def bank_balance(interaction: discord.Interaction) -> None:
    store = guild_store(DATA, interaction.guild_id)
    bank = store["bank"]
    all_tx = bank.get("tx", [])
    txs = all_tx[-10:][::-1]
    total_in = sum(t["amount"] for t in all_tx if t["kind"] == "in")
    total_out = sum(t["amount"] for t in all_tx if t["kind"] == "out")
    flow = total_in + total_out
    embed = discord.Embed(
        title="🏦  Gang Bank",
        description=f"# {_money(bank.get('balance', 0))}",
        color=0x22C55E,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    if all_tx:
        embed.add_field(
            name="📈 Balance trend · last 20 moves",
            value=f"`{_spark([t['balance'] for t in all_tx[-20:]])}`",
            inline=False,
        )
        embed.add_field(
            name="In vs out",
            value=(
                f"📥 `{_bar(total_in, flow)}` {_money(total_in)}\n"
                f"📤 `{_bar(total_out, flow)}` {_money(total_out)}"
            ),
            inline=False,
        )
        embed.add_field(
            name="🧾 Recent",
            value="\n".join(
                f"{'📥' if t['kind'] == 'in' else '📤'} **{_money(t['amount'])}** · <t:{t['ts']}:R> · "
                f"<@{t['by']}>{(' — ' + t['note']) if t['note'] else ''}"
                for t in txs
            )[:1024],
            inline=False,
        )
    else:
        embed.add_field(name="📈 Trend", value="*No transactions yet — `/bank deposit` to start.*", inline=False)
    _brand(embed, interaction.guild, f"{len(all_tx)} transactions")
    await _send_v2(interaction, embed)


# ---- wars ------------------------------------------------------------------- #

def _war_embed(war: dict) -> discord.Embed:
    w, l = war.get("wins", 0), war.get("losses", 0)
    total = w + l
    pct = f"{(w / total) * 100:.0f}%" if total else "—"
    active = war.get("active", True)
    embed = discord.Embed(
        title=f"⚔️  War vs {war['enemy']}" + ("" if active else " — ended"),
        color=0xDC2626 if active else 0x6B7280,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(name="🏆 Wins", value=f"**{w}**", inline=True)
    embed.add_field(name="💀 Losses", value=f"**{l}**", inline=True)
    embed.add_field(name="📈 Win rate", value=pct, inline=True)
    embed.add_field(name="Started", value=f"<t:{war['start']}:R>", inline=True)
    if war.get("log"):
        embed.add_field(
            name="Latest",
            value="\n".join(
                f"{'🏆' if e['r'] == 'win' else '💀'} <t:{e['ts']}:R>{(' — ' + e['note']) if e.get('note') else ''}"
                for e in war["log"][-5:][::-1]
            )[:1024],
            inline=False,
        )
    return embed


def _find_war(store, enemy: str, active_only: bool = True) -> dict | None:
    for war in store["wars"]:
        if war["enemy"].lower() == enemy.lower() and (war.get("active", True) or not active_only):
            return war
    return None


async def war_autocomplete(interaction: discord.Interaction, current: str):
    store = guild_store(DATA, interaction.guild_id)
    return [
        app_commands.Choice(name=w["enemy"], value=w["enemy"])
        for w in store["wars"]
        if w.get("active", True) and current.lower() in w["enemy"].lower()
    ][:25]


@war_group.command(name="start", description="Start tracking a war (leadership)")
@app_commands.describe(enemy="Who you're at war with")
async def war_start(interaction: discord.Interaction, enemy: str) -> None:
    if await _deny(interaction, "start wars"):
        return
    store = guild_store(DATA, interaction.guild_id)
    if _find_war(store, enemy):
        await interaction.response.send_message(f"Already at war with **{enemy}**.", ephemeral=True)
        return
    war = {
        "enemy": enemy.strip()[:60],
        "wins": 0,
        "losses": 0,
        "start": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
        "active": True,
        "log": [],
    }
    store["wars"].append(war)
    save_data(DATA)
    await interaction.response.send_message(embed=_brand(_war_embed(war), interaction.guild, "War declared"))
    await send_log(interaction.guild, store, f"⚔️ War started vs **{war['enemy']}**.",
                   category="kos", actor=interaction.user)


@war_group.command(name="result", description="Log a win or loss in a war (leadership)")
@app_commands.describe(enemy="Which war", result="Did you win or lose", note="Optional detail")
@app_commands.choices(result=[
    app_commands.Choice(name="🏆 Win", value="win"),
    app_commands.Choice(name="💀 Loss", value="loss"),
])
@app_commands.autocomplete(enemy=war_autocomplete)
async def war_result(
    interaction: discord.Interaction, enemy: str, result: app_commands.Choice[str], note: str = ""
) -> None:
    if await _deny(interaction, "log war results"):
        return
    store = guild_store(DATA, interaction.guild_id)
    war = _find_war(store, enemy)
    if war is None:
        await interaction.response.send_message(f"No active war with **{enemy}**.", ephemeral=True)
        return
    war["wins" if result.value == "win" else "losses"] += 1
    war["log"].append({
        "r": result.value,
        "note": note[:120],
        "ts": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
    })
    war["log"] = war["log"][-100:]
    save_data(DATA)
    await interaction.response.send_message(embed=_brand(_war_embed(war), interaction.guild))


@war_group.command(name="end", description="End a war (leadership)")
@app_commands.autocomplete(enemy=war_autocomplete)
async def war_end(interaction: discord.Interaction, enemy: str) -> None:
    if await _deny(interaction, "end wars"):
        return
    store = guild_store(DATA, interaction.guild_id)
    war = _find_war(store, enemy)
    if war is None:
        await interaction.response.send_message(f"No active war with **{enemy}**.", ephemeral=True)
        return
    war["active"] = False
    war["end"] = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    save_data(DATA)
    w, l = war["wins"], war["losses"]
    verdict = "🏆 **Victory**" if w > l else ("💀 **Defeat**" if l > w else "🤝 **Draw**")
    e = _war_embed(war)
    e.description = f"{verdict} — final score **{w}–{l}**"
    await interaction.response.send_message(embed=_brand(e, interaction.guild, "War over"))
    await send_log(interaction.guild, store, f"🏳️ War vs **{war['enemy']}** ended {w}–{l}.",
                   category="kos", actor=interaction.user)


@war_group.command(name="view", description="Show all active wars with win-rate bars")
async def war_view(interaction: discord.Interaction) -> None:
    store = guild_store(DATA, interaction.guild_id)
    active = [w for w in store["wars"] if w.get("active", True)]
    embed = discord.Embed(
        title="⚔️  Active Wars", color=0xDC2626,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    if not active:
        embed.description = "☮️ No active wars."
    for w in active[:20]:
        total = w["wins"] + w["losses"]
        pct = f"{w['wins'] / total * 100:.0f}%" if total else "—"
        form = "".join("🟩" if e["r"] == "win" else "🟥" for e in w.get("log", [])[-10:]) or "—"
        embed.add_field(
            name=f"vs {w['enemy']}  ·  {w['wins']}–{w['losses']}",
            value=f"`{_bar(w['wins'], total, 14)}` {pct} win rate\nForm: {form}\n-# since <t:{w['start']}:R>",
            inline=False,
        )
    _brand(embed, interaction.guild)
    await _send_v2(interaction, embed)


# ---- dashboard + member profile -------------------------------------------- #

def _dashboard_cards(guild: discord.Guild, store: dict[str, Any]) -> list[discord.Embed]:
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    ranked_ids: set[int] = set()
    rank_rows: list[tuple[str, int, int]] = []
    for r in store.get("ranks", []):
        role = guild.get_role(r["role_id"])
        if role is not None:
            members = role.members
            ranked_ids.update(m.id for m in members)
            rank_rows.append((r["name"], len(members), role.position))
    rank_rows.sort(key=lambda x: -x[2])
    orders = store["orders"]
    open_o = [o for o in orders if o.get("status", "open") != "closed"]
    rated = [o["rating"] for o in orders if o.get("rating")]
    recent_strikes = sum(1 for s in store["strikes"] if s["ts"] >= now - 30 * 86400)
    bank = store["bank"]
    txs = bank.get("tx", [])
    active_wars = [w for w in store["wars"] if w.get("active", True)]

    hq = discord.Embed(
        title=f"🐉  {guild.name} — Command Center",
        description=f"-# 🟢 Live · updated <t:{now}:R> · {guild.member_count or len(guild.members)} in server",
        color=EMBED_COLOR,
    )
    if guild.icon:
        hq.set_thumbnail(url=guild.icon.url)
    hq.add_field(name="👥 Ranked", value=f"**{len(ranked_ids)}**")
    hq.add_field(name="💀 On KOS", value=f"**{len(store['kos'])}**")
    hq.add_field(name="⚠️ Strikes 30d", value=f"**{recent_strikes}**")
    hq.add_field(name="📻 Radio", value=f"`{store.get('radio_freq') or '—'}`")
    if guild.banner:
        hq.set_image(url=guild.banner.url)

    total_in = sum(t["amount"] for t in txs if t["kind"] == "in")
    total_out = sum(t["amount"] for t in txs if t["kind"] == "out")
    flow = total_in + total_out
    money = discord.Embed(
        title="🏦  Bank", description=f"# {_money(bank.get('balance', 0))}", color=0x22C55E
    )
    if txs:
        money.add_field(
            name="📈 Balance trend · last 20 moves",
            value=f"`{_spark([t['balance'] for t in txs[-20:]])}`",
            inline=False,
        )
        money.add_field(
            name="In vs out",
            value=(
                f"📥 `{_bar(total_in, flow)}` {_money(total_in)}\n"
                f"📤 `{_bar(total_out, flow)}` {_money(total_out)}"
            ),
            inline=False,
        )
    else:
        money.add_field(name="📈 Trend", value="*No transactions yet — `/bank deposit` to start.*", inline=False)

    daily = _daily_counts([o["created_ts"] for o in orders if o.get("created_ts")])
    avg = f"{sum(rated) / len(rated):.1f}⭐" if rated else "—"
    ops = discord.Embed(
        title="🎫  Tickets",
        description=f"**{len(open_o)}** open · **{len(orders)}** all-time · avg {avg}",
        color=0x0EA5E9,
    )
    ops.add_field(
        name="📊 Opened per day · last 14d",
        value=f"`{_spark(daily)}`  peak **{max(daily)}**/day · **{sum(daily)}** total",
        inline=False,
    )
    if open_o:
        lines = []
        for key, label in reversed(PRIORITIES):
            n = sum(1 for o in open_o if o.get("priority", "normal") == key)
            dot, name = label.split(" ", 1)
            lines.append(f"{dot} `{_bar(n, len(open_o), 10)}` **{n}** {name}")
        ops.add_field(name="🚩 Open by priority", value="\n".join(lines), inline=False)

    wars = discord.Embed(title="⚔️  Wars", color=0xDC2626)
    if active_wars:
        rows = []
        for w in active_wars[:5]:
            total = w["wins"] + w["losses"]
            pct = f"{w['wins'] / total * 100:.0f}%" if total else "—"
            rows.append(f"**vs {w['enemy']}** · {w['wins']}–{w['losses']}\n`{_bar(w['wins'], total, 14)}` {pct} win rate")
        wars.description = "\n".join(rows)
    else:
        wars.description = "☮️ No active wars."

    cards = [hq, money, ops, wars]
    if rank_rows:
        top = max(n for _, n, _ in rank_rows) or 1
        cards.append(
            discord.Embed(
                title="🎖️  Ranks",
                description="\n".join(
                    f"`{_bar(n, top, 10)}` **{n}** · {name}" for name, n, _ in rank_rows[:8]
                ),
                color=0xA855F7,
            )
        )
    _brand(cards[-1], guild, "🔄 tap Refresh for live numbers")
    return cards


class DashboardView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄",
        custom_id="gang:dashboard",
    )
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        store = guild_store(DATA, interaction.guild_id)
        await _edit_card(
            interaction.message,
            _dashboard_cards(interaction.guild, store),
            DashboardView,
            interaction=interaction,
        )


@gang_group.command(name="dashboard", description="Live command center for the whole gang")
async def gang_dashboard(interaction: discord.Interaction) -> None:
    store = guild_store(DATA, interaction.guild_id)
    await _send_card(interaction, _dashboard_cards(interaction.guild, store), DashboardView)


@gang_group.command(name="profile", description="Everything about one member")
@app_commands.describe(member="Who to look up (defaults to you)")
async def gang_profile(interaction: discord.Interaction, member: discord.Member | None = None) -> None:
    member = member or interaction.user
    store = guild_store(DATA, interaction.guild_id)
    rank_names = [
        r["name"] for r in store.get("ranks", [])
        if any(role.id == r["role_id"] for role in member.roles)
    ]
    strikes = [s for s in store["strikes"] if s["user_id"] == member.id]
    limit = store.get("strike_limit", 3)
    tickets = [o for o in store["orders"] if o.get("user_id") == member.id]
    handled = [o for o in store["orders"] if o.get("claimed_by") == member.id and o.get("status") == "closed"]
    on_kos = any(k.get("id") == member.id for k in store["kos"])

    embed = discord.Embed(
        title=member.display_name,
        color=member.color if member.color.value else EMBED_COLOR,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="🎖️ Rank", value=", ".join(rank_names) or "—", inline=True)
    embed.add_field(name="💀 KOS", value="Yes" if on_kos else "No", inline=True)
    embed.add_field(
        name="⚠️ Strikes",
        value=f"**{len(strikes)}/{limit}** " + "🟥" * min(len(strikes), limit) + "⬛" * max(0, limit - len(strikes)),
        inline=True,
    )
    if member.joined_at:
        embed.add_field(name="📅 Joined", value=f"<t:{int(member.joined_at.timestamp())}:R>", inline=True)
    embed.add_field(name="🎫 Tickets opened", value=str(len(tickets)), inline=True)
    if handled:
        r = [o["rating"] for o in handled if o.get("rating")]
        embed.add_field(
            name="🙋 Tickets handled",
            value=f"{len(handled)}" + (f" · {sum(r) / len(r):.1f}⭐" if r else ""),
            inline=True,
        )
    _brand(embed, interaction.guild)
    await _send_v2(interaction, embed)


# export everything (incl. _helpers) to the modules that build on this one
__all__ = [n for n in list(globals()) if not n.startswith("__")]
