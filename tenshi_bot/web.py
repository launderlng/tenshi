"""Optional web roster manager.

A small aiohttp server, run inside the bot process, that lets leadership view
the roster in a browser and remove members — which takes their gang roles off
in Discord through the live bot connection.

It only turns on when BOTH are set:
  * PORT             — Railway sets this for a web service; without it the
                       server stays off and the bot runs exactly as before.
  * WEB_ADMIN_KEY    — a secret you choose. Every page load and API call must
                       present it, so the role-removing endpoints are never
                       open to the public.

Nothing here changes the bot's Discord behaviour; it's purely an extra door
into the same roster data and role actions the /roster commands already use.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time

from .core import *  # noqa: F401,F403

try:  # aiohttp ships with discord.py; guard so the package still imports without it
    from aiohttp import web as _aioweb
except Exception:  # pragma: no cover - only in trimmed test envs
    _aioweb = None

try:
    import aiohttp as _aiohttp
except Exception:  # pragma: no cover
    _aiohttp = None


# --------------------------------------------------------------------------- #
# Roster data / actions (pure-ish; unit-tested without a running server)
# --------------------------------------------------------------------------- #

def _gang_role_ids(guild: "discord.Guild", store: dict) -> set[int]:
    """Every role that counts as 'in the gang': the base roster role + each
    configured rank role."""
    ids: set[int] = set()
    rid = store.get("roster_role_id")
    if rid:
        ids.add(rid)
    for r in store.get("ranks", []):
        if r.get("role_id"):
            ids.add(r["role_id"])
    return ids


def _top_rank_name(guild: "discord.Guild", store: dict, member) -> str | None:
    """The highest-positioned rank the member currently holds, by role position."""
    best_name, best_pos = None, -1
    member_role_ids = {r.id for r in getattr(member, "roles", [])}
    for r in store.get("ranks", []):
        role = guild.get_role(r.get("role_id"))
        if role is not None and role.id in member_role_ids and role.position > best_pos:
            best_name, best_pos = r["name"], role.position
    return best_name


def _manageable(guild, role) -> bool:
    """Can the bot add/remove this role? (its own top role must sit above it)"""
    me = getattr(guild, "me", None)
    return me is not None and role.position < me.top_role.position


def _roster_members(guild: "discord.Guild", store: dict) -> list[dict]:
    """Everyone on the roster: holders of any gang role, plus anyone still on the
    stored roster list. Each gang role is returned as {id,name,manageable} so the
    page can remove individual roles, not just the whole member."""
    role_ids = _gang_role_ids(guild, store)
    found: dict[int, object] = {}

    for rid in role_ids:
        role = guild.get_role(rid)
        if role is not None:
            for m in role.members:
                found[m.id] = m

    for entry in store.get("roster", []):
        mid = entry.get("id")
        if mid and mid not in found:
            m = guild.get_member(mid)
            if m is not None:
                found[mid] = m

    out = []
    for m in found.values():
        roles = [
            {"id": str(r.id), "name": r.name, "manageable": _manageable(guild, r)}
            for r in sorted(getattr(m, "roles", []), key=lambda r: r.position, reverse=True)
            if r.id in role_ids
        ]
        out.append(
            {
                "id": str(m.id),
                "name": m.display_name,
                "avatar": str(m.display_avatar.url),
                "rank": _top_rank_name(guild, store, m),
                "roles": roles,
            }
        )
    out.sort(key=lambda x: x["name"].lower())
    return out


def _rank_options(guild: "discord.Guild", store: dict) -> list[dict]:
    """Gang roles a member can be *given* from the web (base roster role + ranks),
    limited to ones the bot can actually manage."""
    opts, seen = [], set()
    rid = store.get("roster_role_id")
    base = guild.get_role(rid) if rid else None
    if base is not None and _manageable(guild, base):
        opts.append({"id": str(base.id), "name": base.name})
        seen.add(base.id)
    for r in store.get("ranks", []):
        role = guild.get_role(r.get("role_id"))
        if role is not None and role.id not in seen and _manageable(guild, role):
            opts.append({"id": str(role.id), "name": r["name"]})
            seen.add(role.id)
    return opts


def _overview(guild: "discord.Guild", store: dict) -> dict:
    """Live counts for the dashboard strip (mirrors /gang dashboard)."""
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    ranked: set[int] = set()
    for r in store.get("ranks", []):
        role = guild.get_role(r.get("role_id"))
        if role is not None:
            ranked.update(m.id for m in role.members)
    eoi_timers = store.get("eoi_timers", {})
    open_eois = sum(
        1 for mid in store.get("eoi_apps", {}) if not eoi_timers.get(mid, {}).get("done")
    )
    return {
        "ranked": len(ranked),
        "kos": len(store.get("kos", [])),
        "open_eois": open_eois,
        "strikes_30d": sum(
            1 for s in store.get("strikes", []) if s.get("ts", 0) >= now - 30 * 86400
        ),
        "wars": sum(1 for w in store.get("wars", []) if w.get("active", True)),
        "radio": store.get("radio_freq") or "—",
        "members": guild.member_count or len(getattr(guild, "members", [])),
    }


def _kos_members(guild: "discord.Guild", store: dict) -> list[dict]:
    out = []
    for k in store.get("kos", []):
        uid = k.get("id")
        if not uid:
            continue
        m = guild.get_member(uid)
        out.append(
            {
                "id": str(uid),
                "name": m.display_name if m is not None else (k.get("name") or str(uid)),
                "avatar": str(m.display_avatar.url) if m is not None else "",
                "reason": k.get("reason") or "",
            }
        )
    out.sort(key=lambda x: x["name"].lower())
    return out


async def _set_gang_role(guild, store, user_id: int, role_id: int, action: str) -> dict:
    """Add or remove ONE gang role (roster/rank) on a member. Refuses anything
    that isn't a gang role, and anything above the bot."""
    if role_id not in _gang_role_ids(guild, store):
        return {"ok": False, "error": "That isn't a gang role."}
    role = guild.get_role(role_id)
    if role is None:
        return {"ok": False, "error": "Role not found."}
    me = guild.me
    if me is not None and role.position >= me.top_role.position:
        return {"ok": False, "error": f"'{role.name}' sits above my role — I can't change it."}
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            member = None
    if member is None:
        return {"ok": False, "error": "Member not found."}
    try:
        if action == "add":
            await member.add_roles(role, reason="Web roster: add role")
        else:
            await member.remove_roles(role, reason="Web roster: remove role")
    except discord.HTTPException:
        return {"ok": False, "error": "Discord refused the change — check my permissions."}
    save_data(DATA)
    await send_log(
        guild, store,
        f"🌐 {member.mention} — role **{role.name}** {action}ed via the website.",
        category="roster",
    )
    return {"ok": True, "action": action, "role": role.name}


async def _remove_kos(guild, store, user_id: int) -> dict:
    before = len(store.get("kos", []))
    store["kos"] = [k for k in store.get("kos", []) if k.get("id") != user_id]
    was_listed = len(store["kos"]) != before
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            member = None
    rid = store.get("kos_role_id")
    role = guild.get_role(rid) if rid else None
    role_removed = False
    if member is not None and role is not None and role in member.roles:
        try:
            await member.remove_roles(role, reason="Web: remove from KOS")
            role_removed = True
        except discord.HTTPException:
            pass
    save_data(DATA)
    name = member.mention if member is not None else f"`{user_id}`"
    await send_log(guild, store, f"🌐 {name} taken off KOS via the website.", category="kos")
    return {
        "ok": True,
        "was_listed": was_listed,
        "role_removed": role_removed,
        "member": None if member is None else member.display_name,
    }


async def _remove_from_roster(
    guild: "discord.Guild", store: dict, user_id: int, actor: str = "website"
) -> dict:
    """Drop the member from the stored roster AND strip every gang role from them
    in Discord. Mirrors what /roster remove does, but also clears rank roles so
    they actually disappear from the live roster."""
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            member = None

    before = len(store.get("roster", []))
    store["roster"] = [e for e in store.get("roster", []) if e.get("id") != user_id]
    was_listed = len(store["roster"]) != before

    removed_roles: list[str] = []
    failed_roles: list[str] = []
    if member is not None:
        role_ids = _gang_role_ids(guild, store)
        to_remove = [r for r in member.roles if r.id in role_ids]
        me = guild.me
        for role in to_remove:
            # skip roles we can't manage rather than erroring the whole request
            if me is not None and role.position >= me.top_role.position:
                failed_roles.append(role.name)
                continue
            try:
                await member.remove_roles(role, reason=f"Roster remove via website ({actor})")
                removed_roles.append(role.name)
            except discord.HTTPException:
                failed_roles.append(role.name)

    save_data(DATA)

    name = member.mention if member is not None else f"`{user_id}`"
    await send_log(
        guild,
        store,
        f"🌐 {name} removed from the roster via the website.",
        category="roster",
        fields=[
            ("Roles removed", ", ".join(removed_roles) or "—", False),
            ("Couldn't remove", ", ".join(failed_roles) or "—", True),
        ],
    )
    return {
        "ok": True,
        "was_listed": was_listed,
        "removed_roles": removed_roles,
        "failed_roles": failed_roles,
        "member": None if member is None else member.display_name,
    }


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

# ---- sessions (short-lived signed cookie saying who you are) ---------------- #

_COOKIE = "tenshi_sess"
_STATE_COOKIE = "tenshi_state"


def _secret() -> bytes:
    raw = (
        os.getenv("WEB_SECRET")
        or os.getenv("DISCORD_CLIENT_SECRET")
        or os.getenv("DISCORD_TOKEN")
        or "tenshi-dev-secret"
    )
    return hashlib.sha256(raw.encode()).digest()


def _sign_session(uid: int | str, ttl: int = 43200) -> str:
    payload = {"uid": str(uid), "exp": int(time.time()) + ttl}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def _read_session(cookie: str | None) -> str | None:
    if not cookie or "." not in cookie:
        return None
    raw, sig = cookie.rsplit(".", 1)
    good = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, good):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload.get("uid")


# ---- Discord OAuth (identify only; the bot checks the role itself) ---------- #

_OAUTH_AUTHORIZE = "https://discord.com/oauth2/authorize"
_OAUTH_TOKEN = "https://discord.com/api/oauth2/token"
_OAUTH_ME = "https://discord.com/api/users/@me"


def _configured() -> bool:
    return bool(os.getenv("DISCORD_CLIENT_SECRET"))


def _client_id() -> str:
    return str(getattr(getattr(bot, "user", None), "id", "") or "")


def _redirect_uri(request) -> str:
    host = request.headers.get("X-Forwarded-Host") or request.host
    return f"https://{host}/callback"


def _authorize_url(request, state: str) -> str:
    from urllib.parse import urlencode

    q = urlencode(
        {
            "client_id": _client_id(),
            "response_type": "code",
            "scope": "identify",
            "redirect_uri": _redirect_uri(request),
            "state": state,
            "prompt": "none",
        }
    )
    return f"{_OAUTH_AUTHORIZE}?{q}"


async def _oauth_exchange(code: str, redirect_uri: str) -> str | None:
    secret = os.getenv("DISCORD_CLIENT_SECRET")
    if not secret or _aiohttp is None:
        return None
    data = {
        "client_id": _client_id(),
        "client_secret": secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    async with _aiohttp.ClientSession() as s:
        async with s.post(
            _OAUTH_TOKEN,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as r:
            if r.status != 200:
                return None
            return (await r.json()).get("access_token")


async def _oauth_user(token: str) -> dict | None:
    if _aiohttp is None:
        return None
    async with _aiohttp.ClientSession() as s:
        async with s.get(_OAUTH_ME, headers={"Authorization": f"Bearer {token}"}) as r:
            if r.status != 200:
                return None
            return await r.json()


# ---- role-based access ------------------------------------------------------ #

async def _has_web_access(guild, uid) -> bool:
    """True only if the user holds this guild's configured website-access role."""
    store = guild_store(DATA, guild.id)
    rid = store.get("web_role_id")
    if not rid:
        return False
    member = guild.get_member(int(uid))
    if member is None:
        try:
            member = await guild.fetch_member(int(uid))
        except discord.HTTPException:
            return False
    return any(r.id == rid for r in member.roles)


async def _accessible_guilds(uid) -> list:
    out = []
    for g in bot.guilds:
        if await _has_web_access(g, uid):
            out.append(g)
    return out


# ---- HTTP handlers ---------------------------------------------------------- #

def _guild_from(gid: str | None):
    if not gid:
        return bot.guilds[0] if len(bot.guilds) == 1 else None
    try:
        return bot.get_guild(int(gid))
    except (TypeError, ValueError):
        return None


async def _h_index(request):
    return _aioweb.Response(text=_PAGE_HTML, content_type="text/html")


async def _h_me(request):
    if not _configured():
        return _aioweb.json_response({"authed": False, "reason": "not_configured"})
    uid = _read_session(request.cookies.get(_COOKIE))
    if not uid:
        return _aioweb.json_response({"authed": False, "reason": "login"})
    guilds = await _accessible_guilds(uid)
    if not guilds:
        return _aioweb.json_response({"authed": False, "reason": "no_access"})
    return _aioweb.json_response(
        {
            "authed": True,
            "user": {"id": str(uid)},
            "guilds": [{"id": str(g.id), "name": g.name} for g in guilds],
        }
    )


async def _h_login(request):
    if not _configured():
        return _aioweb.Response(
            text="Login isn't set up on this server (DISCORD_CLIENT_SECRET is missing).",
            status=503,
        )
    state = secrets.token_urlsafe(16)
    resp = _aioweb.HTTPFound(_authorize_url(request, state))
    resp.set_cookie(_STATE_COOKIE, state, max_age=600, httponly=True, secure=True, samesite="Lax")
    return resp


async def _h_callback(request):
    if request.query.get("error"):
        return _aioweb.Response(text="Login was cancelled.", status=400)
    state = request.query.get("state")
    if not state or state != request.cookies.get(_STATE_COOKIE):
        return _aioweb.Response(text="Login expired — please try again.", status=400)
    code = request.query.get("code")
    if not code:
        return _aioweb.Response(text="No code returned.", status=400)
    token = await _oauth_exchange(code, _redirect_uri(request))
    if not token:
        return _aioweb.Response(text="Login failed (token exchange).", status=400)
    user = await _oauth_user(token)
    if not user or not user.get("id"):
        return _aioweb.Response(text="Couldn't read your Discord account.", status=400)
    guilds = await _accessible_guilds(user["id"])
    if not guilds:
        return _aioweb.Response(text=_DENIED_HTML, status=403, content_type="text/html")
    resp = _aioweb.HTTPFound("/")
    resp.set_cookie(
        _COOKIE, _sign_session(user["id"]), max_age=43200,
        httponly=True, secure=True, samesite="Lax",
    )
    resp.del_cookie(_STATE_COOKIE)
    return resp


async def _h_logout(request):
    resp = _aioweb.HTTPFound("/")
    resp.del_cookie(_COOKIE)
    return resp


async def _gate(request, guild) -> tuple[bool, int, str]:
    """Allow only a logged-in user who holds this guild's website-access role."""
    if not _configured():
        return (False, 503, "Login isn't configured on this server.")
    uid = _read_session(request.cookies.get(_COOKIE))
    if not uid:
        return (False, 401, "Not logged in.")
    if not await _has_web_access(guild, uid):
        return (False, 403, "You don't have the website-access role.")
    return (True, 200, "")


async def _h_roster(request):
    guild = _guild_from(request.query.get("guild"))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    return _aioweb.json_response(
        {
            "guild": {"id": str(guild.id), "name": guild.name},
            "members": _roster_members(guild, store),
            "ranks": _rank_options(guild, store),
        }
    )


async def _h_overview(request):
    guild = _guild_from(request.query.get("guild"))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    return _aioweb.json_response({"overview": _overview(guild, store)})


async def _h_kos(request):
    guild = _guild_from(request.query.get("guild"))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    return _aioweb.json_response({"members": _kos_members(guild, store)})


async def _h_role(request):
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad JSON body."}, status=400)
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    action = body.get("action")
    if action not in ("add", "remove"):
        return _aioweb.json_response({"error": "action must be add or remove."}, status=400)
    try:
        uid = int(body.get("user_id"))
        rid = int(body.get("role_id"))
    except (TypeError, ValueError):
        return _aioweb.json_response({"error": "Bad user_id or role_id."}, status=400)
    store = guild_store(DATA, guild.id)
    result = await _set_gang_role(guild, store, uid, rid, action)
    return _aioweb.json_response(result, status=200 if result.get("ok") else 400)


async def _h_kos_remove(request):
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad JSON body."}, status=400)
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    try:
        uid = int(body.get("user_id"))
    except (TypeError, ValueError):
        return _aioweb.json_response({"error": "Bad user_id."}, status=400)
    store = guild_store(DATA, guild.id)
    result = await _remove_kos(guild, store, uid)
    return _aioweb.json_response(result)


async def _h_remove(request):
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad JSON body."}, status=400)
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    try:
        uid = int(body.get("user_id"))
    except (TypeError, ValueError):
        return _aioweb.json_response({"error": "Bad user_id."}, status=400)
    store = guild_store(DATA, guild.id)
    actor = _read_session(request.cookies.get(_COOKIE))
    result = await _remove_from_roster(guild, store, uid, actor=f"user {actor}")
    return _aioweb.json_response(result)


_web_started = False


async def start_web_server() -> None:
    """Start the roster web server if PORT is configured. Safe to call once in
    on_ready; a no-op (with a log line) when disabled or aiohttp.web is absent."""
    global _web_started
    if _web_started:
        return
    port = os.getenv("PORT", "")
    if not port.isdigit():
        log.info("PORT not set — web roster manager is off.")
        return
    if _aioweb is None:
        log.warning("aiohttp.web unavailable — web roster manager is off.")
        return
    if not _configured():
        log.warning(
            "PORT is set but DISCORD_CLIENT_SECRET is not — the website loads but "
            "nobody can log in until you set it (and a Website access role)."
        )
    app = _aioweb.Application()
    app.add_routes(
        [
            _aioweb.get("/", _h_index),
            _aioweb.get("/login", _h_login),
            _aioweb.get("/callback", _h_callback),
            _aioweb.get("/logout", _h_logout),
            _aioweb.get("/api/me", _h_me),
            _aioweb.get("/api/overview", _h_overview),
            _aioweb.get("/api/roster", _h_roster),
            _aioweb.get("/api/kos", _h_kos),
            _aioweb.post("/api/role", _h_role),
            _aioweb.post("/api/remove", _h_remove),
            _aioweb.post("/api/kos_remove", _h_kos_remove),
        ]
    )
    runner = _aioweb.AppRunner(app)
    await runner.setup()
    site = _aioweb.TCPSite(runner, "0.0.0.0", int(port))
    await site.start()
    _web_started = True
    log.info("🌐 Roster web manager listening on :%s", port)


_DENIED_HTML = (
    "<!doctype html><meta charset=utf-8><title>No access</title>"
    "<body style='background:#0b0b0d;color:#ececf1;font-family:sans-serif;"
    "display:flex;min-height:100vh;align-items:center;justify-content:center;text-align:center'>"
    "<div><h2>🚫 No access</h2>"
    "<p style='color:#9a9aa6'>Your Discord account doesn't have the website-access role.<br>"
    "Ask leadership for the role set in <code>/settings role → Website access role</code>.</p>"
    "<p><a style='color:#e11d48' href='/logout'>Log out</a></p></div></body>"
)


# --------------------------------------------------------------------------- #
# The page (vanilla JS, no external deps)
# --------------------------------------------------------------------------- #

_PAGE_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tenshi · Roster</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Space+Grotesk:wght@700&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#07070a; --card:rgba(20,20,26,.72); --cardsolid:#15151b; --line:rgba(255,255,255,.07);
    --text:#f2f2f6; --mute:#9a9aa8; --brand:#e11d48; --brand2:#f43f5e;
    --ok:#22c55e; --bad:#ef4444; --warn:#f59e0b; --blue:#60a5fa; --purple:#a78bfa; --r:18px;
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{background:var(--bg);color:var(--text);min-height:100vh;
    font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    -webkit-font-smoothing:antialiased}
  body::before{content:"";position:fixed;inset:-20%;z-index:-1;pointer-events:none;
    background:
      radial-gradient(900px 520px at 12% -8%, rgba(225,29,72,.22), transparent 60%),
      radial-gradient(760px 520px at 102% 2%, rgba(130,20,70,.20), transparent 55%),
      radial-gradient(640px 640px at 50% 118%, rgba(60,10,36,.28), transparent 62%);
    animation:drift 20s ease-in-out infinite alternate}
  @keyframes drift{to{transform:translate3d(0,-14px,0) scale(1.04)}}
  a{text-decoration:none;color:inherit}
  ::selection{background:rgba(225,29,72,.35)}

  header{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px;
    padding:16px 22px;border-bottom:1px solid var(--line);
    background:linear-gradient(180deg,rgba(7,7,10,.92),rgba(7,7,10,.72));backdrop-filter:blur(12px)}
  .logo{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;font-size:18px;
    background:linear-gradient(135deg,#2a0b16,#4c0f22);border:1px solid rgba(244,63,94,.4);
    box-shadow:0 0 22px rgba(225,29,72,.45)}
  .brand{font-family:"Space Grotesk",sans-serif;font-size:19px;font-weight:700;letter-spacing:.5px;
    background:linear-gradient(90deg,#fff,#ffb3c1);-webkit-background-clip:text;background-clip:text;color:transparent;flex:1}
  .brand small{font-family:"Inter";font-weight:500;font-size:12px;color:var(--mute);letter-spacing:0;display:block;margin-top:-2px}

  .wrap{max-width:900px;margin:0 auto;padding:20px 18px 60px}
  button{font:inherit;cursor:pointer;border:1px solid var(--line);background:rgba(255,255,255,.04);
    color:var(--text);padding:9px 14px;border-radius:11px;transition:.16s ease}
  button:hover{border-color:rgba(244,63,94,.55);background:rgba(244,63,94,.1);transform:translateY(-1px)}
  button:active{transform:translateY(0)}
  button:disabled{opacity:.5;cursor:default;transform:none}
  button.brand-btn{background:linear-gradient(135deg,var(--brand),#be123c);border-color:transparent;
    color:#fff;font-weight:700;box-shadow:0 8px 24px rgba(225,29,72,.35)}
  button.ghost{background:transparent}
  button.danger{border-color:rgba(239,68,68,.3);color:#ffb0b0;background:transparent}
  button.danger:hover{background:var(--bad);border-color:var(--bad);color:#fff}
  input[type=text],select{font:inherit;background:rgba(255,255,255,.04);border:1px solid var(--line);
    color:var(--text);padding:11px 14px;border-radius:12px;outline:none;transition:.16s}
  input:focus,select:focus{border-color:rgba(244,63,94,.55);background:rgba(244,63,94,.06)}
  .muted{color:var(--mute)}

  .gate{max-width:440px;margin:14vh auto;text-align:center;padding:30px 26px;border-radius:22px;
    background:var(--card);border:1px solid var(--line);backdrop-filter:blur(14px);box-shadow:0 20px 60px rgba(0,0,0,.5)}
  .gate .logo{margin:0 auto 14px;width:52px;height:52px;font-size:26px;border-radius:16px}
  .gate h2{margin:0 0 6px;font-size:20px} .gate p{margin:0;color:var(--mute)}
  #gateActions{margin-top:20px}

  /* stat tiles */
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:12px;margin:4px 0 20px}
  .stat{position:relative;overflow:hidden;background:var(--card);border:1px solid var(--line);border-radius:16px;
    padding:14px 16px;backdrop-filter:blur(10px);transition:.2s;animation:rise .5s both}
  .stat:hover{transform:translateY(-3px);border-color:rgba(244,63,94,.4)}
  .stat .n{font-size:26px;font-weight:800;line-height:1;font-variant-numeric:tabular-nums}
  .stat .l{font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:.7px;margin-top:6px;font-weight:600}
  .stat .ic{position:absolute;right:10px;top:9px;font-size:16px;opacity:.5}
  .stat::after{content:"";position:absolute;left:0;right:0;bottom:0;height:3px;background:var(--brand);opacity:.8}
  .stat.brand .n{color:#ff7a93} .stat.brand::after{background:var(--brand)}
  .stat.bad .n{color:#ff8a8a} .stat.bad::after{background:var(--bad)}
  .stat.warn .n{color:#ffcf6b} .stat.warn::after{background:var(--warn)}
  .stat.ok .n{color:#86efac} .stat.ok::after{background:var(--ok)}
  .stat.blue .n{color:#93c5fd} .stat.blue::after{background:var(--blue)}
  .stat.purple .n{color:#c4b5fd} .stat.purple::after{background:var(--purple)}
  @keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}

  /* tabs */
  .tabs{display:inline-flex;gap:4px;padding:4px;border-radius:14px;background:rgba(255,255,255,.04);
    border:1px solid var(--line);margin-bottom:16px}
  .tab{padding:8px 18px;border-radius:10px;cursor:pointer;color:var(--mute);font-weight:600;transition:.18s}
  .tab.active{background:linear-gradient(135deg,var(--brand),#be123c);color:#fff;box-shadow:0 6px 18px rgba(225,29,72,.4)}

  .toolbar{display:flex;gap:10px;margin-bottom:16px;align-items:center}
  .toolbar input{flex:1}
  .count{font-size:13px;color:var(--mute);margin-left:auto;white-space:nowrap}

  .ranklabel{display:flex;align-items:center;gap:10px;margin:22px 2px 10px;font-weight:700;font-size:12px;
    text-transform:uppercase;letter-spacing:1px;color:#ffb3c1}
  .ranklabel::before{content:"";width:3px;height:15px;border-radius:3px;background:linear-gradient(var(--brand),#be123c)}
  .ranklabel .pill{margin-left:auto;background:rgba(255,255,255,.05);border:1px solid var(--line);
    border-radius:999px;padding:2px 10px;font-size:11px;color:var(--mute)}

  ul.members{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:9px}
  li.member{display:flex;align-items:center;gap:13px;background:var(--card);border:1px solid var(--line);
    border-radius:15px;padding:12px 14px;backdrop-filter:blur(8px);transition:.18s;animation:rise .4s both}
  li.member:hover{border-color:rgba(244,63,94,.35);background:rgba(255,255,255,.04)}
  li.member .av{width:44px;height:44px;border-radius:50%;flex:none;padding:2px;
    background:linear-gradient(135deg,var(--brand),#7c1d3a)}
  li.member .av img{width:100%;height:100%;border-radius:50%;object-fit:cover;background:#000;display:block}
  li.member .av.skull{display:grid;place-items:center;font-size:20px;background:linear-gradient(135deg,#3a0f0f,#5c1212)}
  .mname{font-weight:700;font-size:15px}
  .grow{flex:1;min-width:0}
  .chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:5px;align-items:center}
  .chip{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:600;padding:3px 10px;border-radius:999px;
    background:rgba(225,29,72,.12);border:1px solid rgba(244,63,94,.28);color:#ffb3c1;transition:.15s}
  .chip.rankrole{background:rgba(96,165,250,.12);border-color:rgba(96,165,250,.3);color:#bcd4ff}
  .chip .x{cursor:pointer;opacity:0;width:0;overflow:hidden;transition:.15s;font-weight:800}
  li.member:hover .chip .x{opacity:.75;width:11px;margin-left:1px}
  .chip .x:hover{opacity:1;color:#fff}
  .chip.locked{opacity:.55}
  .addsel{background:rgba(255,255,255,.03);border:1px dashed var(--line);color:var(--mute);
    border-radius:999px;padding:3px 10px;font-size:11px;cursor:pointer}
  .reason{font-size:12px;color:var(--mute);margin-top:3px}

  .empty{text-align:center;color:var(--mute);padding:46px 0}
  .spinner{width:26px;height:26px;border-radius:50%;border:3px solid rgba(255,255,255,.12);
    border-top-color:var(--brand);animation:spin .7s linear infinite;margin:40px auto}
  @keyframes spin{to{transform:rotate(360deg)}}

  /* toasts */
  #toasts{position:fixed;top:14px;right:14px;z-index:60;display:flex;flex-direction:column;gap:8px;max-width:360px}
  .toast{padding:12px 15px;border-radius:13px;font-size:14px;font-weight:500;color:#fff;
    background:rgba(20,20,26,.92);border:1px solid var(--line);backdrop-filter:blur(10px);
    box-shadow:0 10px 30px rgba(0,0,0,.5);animation:toastin .25s ease}
  .toast.ok{border-color:rgba(34,197,94,.45);background:rgba(16,40,24,.92)}
  .toast.err{border-color:rgba(239,68,68,.5);background:rgba(42,16,16,.92)}
  @keyframes toastin{from{opacity:0;transform:translateX(20px)}to{opacity:1;transform:none}}
  @media(max-width:560px){#toasts{left:14px;right:14px;max-width:none}}
  footer{color:var(--mute);font-size:12px;text-align:center;padding:30px 18px}
</style>
</head>
<body>
<header>
  <div class="logo">🐉</div>
  <div class="brand">TENSHI<small id="sub">Roster Manager</small></div>
  <a id="logoutBtn" href="/logout" style="display:none"><button class="ghost">Log out</button></a>
</header>

<div id="toasts"></div>

<div id="gate" class="gate" style="display:none">
  <div class="logo">🐉</div>
  <h2 id="gateTitle">Sign in</h2>
  <p id="gateText" class="muted">Log in with Discord to manage the roster.</p>
  <div id="gateActions"></div>
</div>

<div id="app" class="wrap" style="display:none">
  <div id="guildRow" style="margin-bottom:14px;display:none">
    <label class="muted" for="guildSel" style="margin-right:8px">Server</label>
    <select id="guildSel" onchange="reload()"></select>
  </div>

  <div id="stats" class="stats"></div>

  <div class="tabs">
    <div class="tab active" data-tab="roster" onclick="setTab('roster')">👥 Roster</div>
    <div class="tab" data-tab="kos" onclick="setTab('kos')">💀 KOS</div>
  </div>

  <div class="toolbar">
    <input id="search" type="text" placeholder="Search members…" oninput="render()">
    <button onclick="reload()">↻ Refresh</button>
    <span id="count" class="count"></span>
  </div>

  <div id="rosterView"></div>
  <div id="kosView" style="display:none"></div>
</div>

<footer>Only people with the website-access role can open this · changes apply to Discord live · 天使</footer>

<script>
let STATE={members:[],ranks:[],kos:[],guild:""},TAB="roster";
const $=id=>document.getElementById(id);
function h(s){return (s||"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function show(id,on){$(id).style.display=on?"":"none";}
function toast(text,kind){
  const t=document.createElement("div");t.className="toast "+(kind||"");t.textContent=text;
  $("toasts").appendChild(t);
  setTimeout(()=>{t.style.transition="opacity .3s";t.style.opacity="0";setTimeout(()=>t.remove(),300);},3800);
}
async function api(path,opts){
  const r=await fetch(path,opts||{});
  let d={};try{d=await r.json();}catch(e){}
  if(!r.ok) throw Object.assign(new Error(d.error||("HTTP "+r.status)),{status:r.status});
  return d;
}
function gate(title,text,actions){
  show("app",false);show("gate",true);$("logoutBtn").style.display="none";
  $("gateTitle").textContent=title;$("gateText").textContent=text;$("gateActions").innerHTML=actions||"";
}
async function boot(){
  let me;try{me=await api("/api/me");}catch(e){gate("Error","Couldn't reach the server. Try again.");return;}
  if(!me.authed){
    if(me.reason==="not_configured") gate("Not set up","The owner still needs to set DISCORD_CLIENT_SECRET and a Website access role.");
    else if(me.reason==="no_access") gate("No access","Your account doesn't have the website-access role. Ask leadership for it.",'<a href="/logout"><button>Log out</button></a>');
    else gate("Sign in","Log in with Discord to manage the roster.",'<a href="/login"><button class="brand-btn" style="width:100%;padding:12px">Log in with Discord</button></a>');
    return;
  }
  show("gate",false);show("app",true);$("logoutBtn").style.display="";
  const sel=$("guildSel");
  sel.innerHTML=me.guilds.map(x=>`<option value="${h(x.id)}">${h(x.name)}</option>`).join("");
  show("guildRow",me.guilds.length>1);
  STATE.guild=me.guilds.length?me.guilds[0].id:"";
  await reload();
}
async function reload(){
  const sel=$("guildSel");if(sel&&sel.value)STATE.guild=sel.value;
  const g=encodeURIComponent(STATE.guild);
  try{
    const [ov,ro,ko]=await Promise.all([api("/api/overview?guild="+g),api("/api/roster?guild="+g),api("/api/kos?guild="+g)]);
    $("sub").textContent=ro.guild?.name||"Roster";
    renderStats(ov.overview);
    STATE.members=ro.members||[];STATE.ranks=ro.ranks||[];STATE.kos=ko.members||[];
    render();
  }catch(e){ if(e.status===401||e.status===403) return boot(); toast(e.message||"Failed to load.","err"); }
}
function countUp(el,to){
  to=Number(to); if(isNaN(to)){el.textContent=arguments[1];return;}
  const dur=600,t0=performance.now();
  (function step(t){const p=Math.min((t-t0)/dur,1);el.textContent=Math.round(to*(1-Math.pow(1-p,3)));if(p<1)requestAnimationFrame(step);})(t0);
}
function renderStats(o){
  const tiles=[["ranked","Ranked","🎯","brand"],["kos","On KOS","💀","bad"],["open_eois","Open EOIs","📨","warn"],
    ["strikes_30d","Strikes 30d","⚠️","warn"],["wars","Wars","⚔️","blue"],["members","In server","👥","ok"]];
  $("stats").innerHTML=tiles.map(([k,l,ic,c],i)=>`<div class="stat ${c}" style="animation-delay:${i*45}ms"><span class="ic">${ic}</span><div class="n" data-c="${h(String(o[k]??'—'))}">0</div><div class="l">${l}</div></div>`).join("")
    +`<div class="stat purple" style="animation-delay:270ms"><span class="ic">📻</span><div class="n" style="font-size:17px">${h(o.radio||'—')}</div><div class="l">Radio</div></div>`;
  document.querySelectorAll('.stat .n[data-c]').forEach(e=>countUp(e,e.dataset.c));
}
function setTab(t){
  TAB=t;document.querySelectorAll(".tab").forEach(e=>e.classList.toggle("active",e.dataset.tab===t));
  show("rosterView",t==="roster");show("kosView",t==="kos");
  $("search").placeholder=t==="roster"?"Search members…":"Search KOS…";render();
}
function render(){TAB==="roster"?renderRoster():renderKos();}
function renderRoster(){
  const q=$("search").value.toLowerCase();
  let ms=STATE.members.filter(m=>m.name.toLowerCase().includes(q)||(m.rank||"").toLowerCase().includes(q));
  $("count").textContent=STATE.members.length+" member"+(STATE.members.length===1?"":"s");
  if(!ms.length){$("rosterView").innerHTML='<div class="empty">No members match.</div>';return;}
  const groups={};ms.forEach(m=>{const k=m.rank||"Unranked";(groups[k]=groups[k]||[]).push(m);});
  const names=Object.keys(groups).sort((a,b)=>a==="Unranked"?1:b==="Unranked"?-1:groups[b].length-groups[a].length||a.localeCompare(b));
  $("rosterView").innerHTML=names.map(rk=>`<div class="ranklabel">${h(rk)}<span class="pill">${groups[rk].length}</span></div><ul class="members">${groups[rk].map(memberRow).join("")}</ul>`).join("");
}
function memberRow(m){
  const chips=(m.roles||[]).map(r=>{
    const cls="chip "+(STATE.ranks.some(x=>x.name===r.name)?"rankrole":"")+(r.manageable?"":" locked");
    const x=r.manageable?`<span class="x" title="Remove role" onclick="removeRole('${h(m.id)}','${h(r.id)}','${h(r.name)}')">✕</span>`:`<span class="x" style="opacity:.6;width:11px" title="Above the bot">🔒</span>`;
    return `<span class="${cls}">${h(r.name)}${x}</span>`;
  }).join("");
  const have=new Set((m.roles||[]).map(r=>r.name));
  const addable=STATE.ranks.filter(x=>!have.has(x.name));
  const addsel=addable.length?`<select class="addsel" onchange="addRole('${h(m.id)}',this.value,this);this.value=''"><option value="">+ role</option>${addable.map(x=>`<option value="${h(x.id)}">${h(x.name)}</option>`).join("")}</select>`:"";
  return `<li class="member" data-id="${h(m.id)}"><div class="av"><img src="${h(m.avatar)}" alt="" loading="lazy"></div>
    <div class="grow"><div class="mname">${h(m.name)}</div><div class="chips">${chips||'<span class="chip locked">no gang roles</span>'}${addsel}</div></div>
    <button class="danger" onclick="removeMember('${h(m.id)}','${h(m.name)}')">Remove all</button></li>`;
}
function renderKos(){
  const q=$("search").value.toLowerCase();
  const ks=STATE.kos.filter(k=>k.name.toLowerCase().includes(q));
  $("count").textContent=STATE.kos.length+" on KOS";
  if(!ks.length){$("kosView").innerHTML='<div class="empty">Nobody on the KOS list. 🕊️</div>';return;}
  $("kosView").innerHTML=`<ul class="members">${ks.map(k=>`<li class="member" data-id="${h(k.id)}">
    ${k.avatar?`<div class="av"><img src="${h(k.avatar)}" alt="" loading="lazy"></div>`:'<div class="av skull">💀</div>'}
    <div class="grow"><div class="mname">${h(k.name)}</div>${k.reason?`<div class="reason">${h(k.reason)}</div>`:""}</div>
    <button class="danger" onclick="removeKos('${h(k.id)}','${h(k.name)}')">Take off KOS</button></li>`).join("")}</ul>`;
}
async function post(path,body){return api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});}
async function removeRole(uid,rid,name){
  try{await post("/api/role",{guild:STATE.guild,user_id:uid,role_id:rid,action:"remove"});toast("Removed role "+name,"ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
async function addRole(uid,rid,sel){
  if(!rid)return;const name=sel.options[sel.selectedIndex].text;
  try{await post("/api/role",{guild:STATE.guild,user_id:uid,role_id:rid,action:"add"});toast("Added "+name,"ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
async function removeMember(uid,name){
  if(!confirm("Remove "+name+" from the roster and take ALL their gang roles off in Discord?"))return;
  try{const r=await post("/api/remove",{guild:STATE.guild,user_id:uid});
    const f=(r.failed_roles||[]).length?" · couldn't remove: "+r.failed_roles.join(", "):"";
    toast("Removed "+name+(r.removed_roles?.length?" ("+r.removed_roles.join(", ")+")":"")+f,f?"err":"ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
async function removeKos(uid,name){
  if(!confirm("Take "+name+" off the KOS list and remove the KOS role?"))return;
  try{await post("/api/kos_remove",{guild:STATE.guild,user_id:uid});toast("Took "+name+" off KOS","ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
boot();
</script>
</body>
</html>
"""


# export for the entrypoint (start_web_server) via star import
__all__ = [n for n in list(globals()) if not n.startswith("__")]
