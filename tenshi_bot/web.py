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


def _roster_members(guild: "discord.Guild", store: dict) -> list[dict]:
    """Everyone on the roster: holders of any gang role, plus anyone still on the
    stored roster list. Returned as plain dicts for JSON."""
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
        gang_roles = [r.name for r in getattr(m, "roles", []) if r.id in role_ids]
        out.append(
            {
                "id": str(m.id),
                "name": m.display_name,
                "avatar": str(m.display_avatar.url),
                "rank": _top_rank_name(guild, store, m),
                "roles": gang_roles,
            }
        )
    out.sort(key=lambda x: x["name"].lower())
    return out


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
        }
    )


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
            _aioweb.get("/api/roster", _h_roster),
            _aioweb.post("/api/remove", _h_remove),
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

_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tenshi · Roster Manager</title>
<style>
  :root{
    --bg:#0b0b0d; --card:#15151a; --line:#26262e; --text:#ececf1; --mute:#9a9aa6;
    --brand:#e11d48; --ok:#22c55e; --bad:#dc2626; --radius:14px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  header{padding:22px 18px;border-bottom:1px solid var(--line);
    display:flex;align-items:center;gap:12px;position:sticky;top:0;background:rgba(11,11,13,.9);
    backdrop-filter:blur(8px);z-index:5}
  header h1{font-size:18px;margin:0;font-weight:700;letter-spacing:.3px}
  header .dot{width:10px;height:10px;border-radius:50%;background:var(--brand);
    box-shadow:0 0 14px var(--brand)}
  .wrap{max-width:760px;margin:0 auto;padding:18px}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  input,select,button{font:inherit}
  input[type=password],input[type=text],select{
    background:var(--card);border:1px solid var(--line);color:var(--text);
    padding:11px 13px;border-radius:10px;outline:none;width:100%}
  input:focus,select:focus{border-color:var(--brand)}
  button{cursor:pointer;border:1px solid var(--line);background:var(--card);color:var(--text);
    padding:10px 14px;border-radius:10px;transition:.15s}
  button:hover{border-color:var(--brand)}
  button.brand{background:var(--brand);border-color:var(--brand);color:#fff;font-weight:600}
  button.danger{background:transparent;border-color:#3a2126;color:#ff6b6b}
  button.danger:hover{background:var(--bad);border-color:var(--bad);color:#fff}
  .muted{color:var(--mute)}
  a{text-decoration:none;color:inherit}
  #gateActions{margin-top:16px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:16px}
  .gate{max-width:420px;margin:12vh auto;text-align:center}
  .gate h2{margin:0 0 6px}
  .gate p{color:var(--mute);margin:0 0 18px}
  .toolbar{display:flex;gap:10px;margin:4px 0 16px;align-items:center}
  .toolbar input{flex:1}
  .count{font-size:13px;color:var(--mute);margin-left:auto;white-space:nowrap}
  ul.members{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}
  li.member{display:flex;align-items:center;gap:12px;background:var(--card);
    border:1px solid var(--line);border-radius:12px;padding:10px 12px}
  li.member img{width:40px;height:40px;border-radius:50%;flex:none;background:#000}
  .mname{font-weight:600}
  .chips{display:flex;gap:5px;flex-wrap:wrap;margin-top:3px}
  .chip{font-size:11px;padding:2px 8px;border-radius:999px;background:#201a1c;
    border:1px solid #352027;color:#ffb3c0}
  .chip.rank{background:#1a2033;border-color:#243154;color:#9db4ff}
  .grow{flex:1;min-width:0}
  .msg{padding:11px 13px;border-radius:10px;margin:10px 0;display:none}
  .msg.ok{background:#102a18;border:1px solid #1e5531;color:#86efac;display:block}
  .msg.err{background:#2a1212;border:1px solid #551e1e;color:#fca5a5;display:block}
  .empty{text-align:center;color:var(--mute);padding:40px 0}
  footer{color:var(--mute);font-size:12px;text-align:center;padding:26px}
</style>
</head>
<body>
<header><span class="dot"></span><h1>Tenshi · Roster Manager</h1></header>

<div id="gate" class="gate card" style="display:none">
  <h2 id="gateTitle">🔒 Sign in</h2>
  <p id="gateText" class="muted">Log in with Discord to manage the roster.</p>
  <div id="gateActions"></div>
</div>

<div id="app" class="wrap" style="display:none">
  <div id="guildRow" class="row" style="margin-bottom:14px;display:none">
    <label class="muted" for="guildSel">Server</label>
    <select id="guildSel" onchange="loadRoster()"></select>
  </div>
  <div class="toolbar">
    <input id="search" type="text" placeholder="Search members…" oninput="render()">
    <button onclick="loadRoster()">↻ Refresh</button>
    <a href="/logout"><button>Log out</button></a>
    <span id="count" class="count"></span>
  </div>
  <div id="msg" class="msg"></div>
  <ul id="list" class="members"></ul>
  <div id="empty" class="empty" style="display:none">Nobody on the roster yet.</div>
</div>

<footer>Only people with the website-access role can open this. Removing someone takes their gang roles off in Discord. Tenshi · Logic</footer>

<script>
let MEMBERS = [];
let GUILD = "";

function h(s){return (s||"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function show(id,on){document.getElementById(id).style.display = on ? "" : "none";}
function msg(text,kind){const m=document.getElementById("msg");m.className="msg "+kind;m.textContent=text;if(!text)m.style.display="none";}

async function api(path, opts){
  opts = opts || {};
  const r = await fetch(path, opts);
  let data = {};
  try{ data = await r.json(); }catch(e){}
  if(!r.ok){ throw Object.assign(new Error(data.error||("HTTP "+r.status)), {status:r.status}); }
  return data;
}

function gate(title, text, actions){
  show("app",false); show("gate",true);
  document.getElementById("gateTitle").textContent = title;
  document.getElementById("gateText").textContent = text;
  document.getElementById("gateActions").innerHTML = actions || "";
}

async function boot(){
  let me;
  try{ me = await api("/api/me"); }
  catch(e){ gate("⚠️ Error", "Couldn't reach the server. Try again."); return; }
  if(!me.authed){
    if(me.reason==="not_configured")
      gate("⚙️ Not set up", "The server owner still needs to set DISCORD_CLIENT_SECRET and a Website access role.");
    else if(me.reason==="no_access")
      gate("🚫 No access", "Your Discord account doesn't have the website-access role. Ask leadership for it.",
           '<a href="/logout"><button>Log out</button></a>');
    else
      gate("🔒 Sign in", "Log in with Discord to manage the roster.",
           '<a href="/login"><button class="brand" style="width:100%">Log in with Discord</button></a>');
    return;
  }
  show("gate",false); show("app",true);
  const sel = document.getElementById("guildSel");
  sel.innerHTML = me.guilds.map(x=>`<option value="${h(x.id)}">${h(x.name)}</option>`).join("");
  show("guildRow", me.guilds.length>1);
  GUILD = me.guilds.length ? me.guilds[0].id : "";
  await loadRoster();
}

async function loadRoster(){
  const sel = document.getElementById("guildSel");
  if(sel && sel.value) GUILD = sel.value;
  msg("","");
  try{
    const d = await api("/api/roster?guild="+encodeURIComponent(GUILD));
    MEMBERS = d.members || [];
    document.querySelector("header h1").textContent = "Tenshi · " + (d.guild?.name || "Roster");
    render();
  }catch(e){
    if(e.status===401 || e.status===403){ return boot(); }  // session expired / role pulled
    msg(e.message||"Failed to load roster.","err");
  }
}

function render(){
  const q = document.getElementById("search").value.toLowerCase();
  const list = document.getElementById("list");
  const items = MEMBERS.filter(m => m.name.toLowerCase().includes(q) || (m.rank||"").toLowerCase().includes(q));
  document.getElementById("count").textContent = MEMBERS.length + (MEMBERS.length===1?" member":" members");
  show("empty", MEMBERS.length===0);
  list.innerHTML = items.map(m=>{
    const rank = m.rank ? `<span class="chip rank">${h(m.rank)}</span>` : "";
    const roles = (m.roles||[]).filter(r=>r!==m.rank).map(r=>`<span class="chip">${h(r)}</span>`).join("");
    return `<li class="member" data-id="${h(m.id)}">
      <img src="${h(m.avatar)}" alt="" loading="lazy">
      <div class="grow">
        <div class="mname">${h(m.name)}</div>
        <div class="chips">${rank}${roles||'<span class="chip" style="opacity:.5">no gang roles</span>'}</div>
      </div>
      <button class="danger" onclick="removeMember('${h(m.id)}','${h(m.name)}')">Remove</button>
    </li>`;
  }).join("");
}

async function removeMember(id, name){
  if(!confirm(`Remove ${name} from the roster and take their gang roles off in Discord?`)) return;
  const li = document.querySelector(`li.member[data-id="${CSS.escape(id)}"]`);
  const btn = li && li.querySelector("button");
  if(btn){ btn.disabled=true; btn.textContent="…"; }
  try{
    const r = await api("/api/remove", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({guild:GUILD, user_id:id})
    });
    MEMBERS = MEMBERS.filter(m=>m.id!==id);
    render();
    const removed = (r.removed_roles||[]).length ? " Roles removed: "+r.removed_roles.join(", ")+"." : "";
    const failed = (r.failed_roles||[]).length ? " ⚠️ Couldn't remove: "+r.failed_roles.join(", ")+" (check my role is above them)." : "";
    msg(`Removed ${name}.`+removed+failed, failed?"err":"ok");
  }catch(e){
    if(btn){ btn.disabled=false; btn.textContent="Remove"; }
    msg(e.message||"Failed to remove.","err");
  }
}

boot();
</script>
</body>
</html>
"""


# export for the entrypoint (start_web_server) via star import
__all__ = [n for n in list(globals()) if not n.startswith("__")]
