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
import re
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


# Fixed rank hierarchy for the website, highest first. Ranks not listed here
# show after these, ordered by their Discord position. Edit this list to reorder.
_RANK_ORDER = ["bot", "gov", "shodai", "leadership", "hierarchy", "honorary", "tenshi"]
# Roles never shown on the website — not as chips, rank groups, or add options.
_HIDDEN_ROLES = {".", "cee fuck", "ceefu"}


def _hidden_role(name: str) -> bool:
    return (name or "").strip().lower() in _HIDDEN_ROLES


def _rank_idx(name: str) -> int:
    """Index in the fixed hierarchy (0 = top); unlisted ranks sort after."""
    try:
        return _RANK_ORDER.index((name or "").strip().lower())
    except ValueError:
        return len(_RANK_ORDER)


def _manageable(guild, role) -> bool:
    """Can the bot add/remove this role? (its own top role must sit above it)"""
    me = getattr(guild, "me", None)
    return me is not None and role.position < me.top_role.position


def _top_rank_name(guild: "discord.Guild", store: dict, member) -> str | None:
    """The member's top rank by the fixed hierarchy (then Discord position),
    among configured ranks they hold, ignoring hidden roles."""
    member_role_ids = {r.id for r in getattr(member, "roles", [])}
    cands = []
    for r in store.get("ranks", []):
        role = guild.get_role(r.get("role_id"))
        name = r.get("name") or (role.name if role is not None else "")
        if (
            role is not None
            and role.id in member_role_ids
            and not _hidden_role(name)
            and not _hidden_role(role.name)
        ):
            cands.append((_rank_idx(name), -role.position, name))
    if not cands:
        return None
    cands.sort()
    return cands[0][2]


def _roster_members(guild: "discord.Guild", store: dict) -> list[dict]:
    """Everyone on the roster: holders of any gang role, plus anyone still on the
    stored roster list. Each gang role is returned as {id,name,manageable},
    ordered by the fixed hierarchy, with hidden roles dropped."""
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
        gang = [
            r
            for r in getattr(m, "roles", [])
            if r.id in role_ids and not _hidden_role(r.name)
        ]
        gang.sort(key=lambda r: (_rank_idx(r.name), -r.position))
        roles = [
            {"id": str(r.id), "name": r.name, "manageable": _manageable(guild, r)}
            for r in gang
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
    limited to ones the bot can manage, hidden roles dropped, ordered by hierarchy."""
    opts, seen = [], set()
    rid = store.get("roster_role_id")
    base = guild.get_role(rid) if rid else None
    if base is not None and _manageable(guild, base) and not _hidden_role(base.name):
        opts.append({"id": str(base.id), "name": base.name, "_i": _rank_idx(base.name)})
        seen.add(base.id)
    for r in store.get("ranks", []):
        role = guild.get_role(r.get("role_id"))
        if (
            role is not None
            and role.id not in seen
            and _manageable(guild, role)
            and not _hidden_role(role.name)
            and not _hidden_role(r.get("name"))
        ):
            opts.append({"id": str(role.id), "name": r["name"], "_i": _rank_idx(r["name"])})
            seen.add(role.id)
    opts.sort(key=lambda o: (o["_i"], o["name"].lower()))
    return [{"id": o["id"], "name": o["name"]} for o in opts]


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


# ---- stash (gang inventory) ------------------------------------------------- #

# A matching emoji per item, picked from keywords in its name. Not the real game
# icon (that needs a trained model), just a quick visual.
_ICON_MAP = [
    (("pistol", "rifle", "smg", "shotgun", "weapon", " gun", "ak", "revolver",
      "knife", "machete", "bat", "sniper"), "🔫"),
    (("ammo", "round", "bullet", " mag", "clip", "shell"), "🧨"),
    (("money", "cash", "dollar", "dirty", "marked", "bank", "chip", "token"), "💵"),
    (("gold", "diamond", "ring", "rolex", "jewel", "necklace", "watch"), "💎"),
    (("weed", "coke", "cocaine", "meth", "crack", "drug", "baggie", "joint",
      "oxy", "pill", "lean", "ket", "acid", "lsd", "shroom"), "💊"),
    (("burger", "taco", "sandwich", "bread", "meat", "food", "donut", "pizza",
      "chips", "apple", "banana"), "🍔"),
    (("water", "soda", "cola", "beer", "juice", "drink", "coffee", "energy", "milk"), "🥤"),
    (("lockpick", "drill", "tool", "screwdriver", "wrench", "hammer", "thermite",
      "advanced", "kit"), "🛠️"),
    (("bandage", "medkit", "medical", "health", "firstaid", "first aid", "heal",
      "painkiller", "morphine"), "🩹"),
    (("phone", "tablet", "laptop", "radio", "gps"), "📱"),
    (("key", "fob"), "🔑"),
    (("cigarette", "cigar", "lighter", "rolling"), "🚬"),
    (("fish", "bait", "rod"), "🐟"),
]


def _item_icon(name: str) -> str:
    n = " " + (name or "").lower()
    for keys, emoji in _ICON_MAP:
        if any(k in n for k in keys):
            return emoji
    return "📦"


def _stash_items(store: dict) -> list[dict]:
    out = [
        {
            "item": s.get("item", ""),
            "qty": int(s.get("qty", 0)),
            "weight": round(float(s.get("weight", 0) or 0), 2),
            "icon": _item_icon(s.get("item", "")),
            "image": s.get("image", "") or "",
        }
        for s in store.get("stash", [])
        if s.get("item")
    ]
    out.sort(key=lambda x: x["item"].lower())
    return out


def _stash_upsert(
    store: dict, item: str, qty, mode: str = "set", weight=None, image=None
) -> dict:
    """Add/set an item in the stash. mode 'add' increments, 'set' replaces.
    Items that drop to 0 or below are removed. `weight` (per-unit kg) and `image`
    (a data: URI icon cropped from a screenshot) are optional; a kept image
    survives when more of the item is added."""
    item = (item or "").strip()
    if not item:
        return {"ok": False, "error": "Item name is required."}
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Quantity must be a whole number."}
    stash = store.setdefault("stash", [])
    hit = None
    for s in stash:
        if s.get("item", "").strip().lower() == item.lower():
            hit = s
            break
    if hit is None:
        hit = {"item": item, "qty": 0, "weight": 0, "image": ""}
        stash.append(hit)
    hit["qty"] = (int(hit.get("qty", 0)) + qty) if mode == "add" else qty
    if weight is not None and str(weight) != "":
        try:
            hit["weight"] = max(float(weight), 0)
        except (TypeError, ValueError):
            pass
    if image and isinstance(image, str) and image.startswith("data:"):
        hit["image"] = image[:200000]  # keep a cropped icon; cap size
    store["stash"] = [s for s in stash if int(s.get("qty", 0)) > 0]
    save_data(DATA)
    return {"ok": True, "item": item, "qty": max(int(hit["qty"]), 0)}


def _stash_remove(store: dict, item: str) -> dict:
    key = (item or "").strip().lower()
    before = len(store.get("stash", []))
    store["stash"] = [
        s for s in store.get("stash", []) if s.get("item", "").strip().lower() != key
    ]
    save_data(DATA)
    return {"ok": len(store["stash"]) != before}


_STASH_SKIP = {
    "inventory", "stash", "trunk", "weight", "items", "item", "qty", "quantity",
    "glovebox", "pocket", "pockets", "name", "amount", "total", "search", "slots",
    "kg", "lb", "capacity", "used", "free",
}
# Weight readouts to ignore: "40/50", "12.5 kg", "40 / 50 KG", etc.
_WEIGHT_RE = re.compile(r"\d+\s*/\s*\d+|\d+(?:[.,]\d+)?\s*(?:kg|kgs|lb|lbs|oz|g)\b", re.I)


def _parse_stash_text(text: str) -> list[dict]:
    """Best-effort: turn OCR'd lines into {item, qty, icon} candidates. Skips the
    weight readout. Imperfect — the page shows these to confirm, never auto-saved."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or _WEIGHT_RE.search(line):  # drop weight bars / per-item weight
            continue
        qty = 1
        m = (
            re.search(r"x\s*(\d{1,6})\b", line, re.I)
            or re.search(r"\b(\d{1,6})\s*x\b", line, re.I)
            or re.search(r"\b(\d{1,6})\b", line)
        )
        if m:
            qty = int(m.group(1))
        name = re.sub(r"\b\d{1,6}\s*x\b|\bx\s*\d{1,6}\b|\b\d{1,6}\b", " ", line, flags=re.I)
        name = re.sub(r"[^A-Za-z0-9 \-'&]", " ", name)
        name = re.sub(r"\s+", " ", name).strip()
        if name.lower() in _STASH_SKIP:
            continue
        # drop OCR garbage: need at least 3 letters and a real word
        if len(re.sub(r"[^A-Za-z]", "", name)) < 3:
            continue
        if not any(len(w) >= 3 for w in name.split()):
            continue
        out.append({"item": name.title(), "qty": qty})
    # merge duplicates
    merged: dict[str, int] = {}
    for c in out:
        merged[c["item"]] = merged.get(c["item"], 0) + c["qty"]
    return [{"item": k, "qty": v, "icon": _item_icon(k)} for k, v in merged.items()]


def _ocr_available() -> bool:
    """True only if the Tesseract engine is actually callable."""
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _ocr_image(data: bytes) -> str:
    """OCR an uploaded screenshot. Returns '' if OCR tooling isn't available.
    Game inventories are usually light text on a dark background, which Tesseract
    reads badly, so dark images are inverted first."""
    try:
        import io as _io

        import pytesseract
        from PIL import Image, ImageOps

        img = Image.open(_io.BytesIO(data)).convert("RGB")
        g = ImageOps.grayscale(img)
        big = max(g.size) or 1
        if big < 2000:  # upscale small screenshots so small item text is legible
            scale = 2000 / big
            g = g.resize((max(int(g.width * scale), 1), max(int(g.height * scale), 1)))
        g = ImageOps.autocontrast(g)
        # invert when mostly dark (light-on-dark UI) -> dark-on-light for Tesseract
        hist = g.histogram()
        if sum(hist[:128]) > sum(hist[128:]):
            g = ImageOps.invert(g)
        best = ""
        for psm in (6, 11, 4):  # block, sparse, column — keep whichever reads most
            try:
                out = pytesseract.image_to_string(g, config=f"--psm {psm}")
            except Exception:
                continue
            if len(out.strip()) > len(best.strip()):
                best = out
        return best
    except Exception as exc:  # tesseract missing, bad image, etc.
        log.info("OCR unavailable/failed: %s", exc)
        return ""


def _trim_bg(img, tol: int = 96):
    """Fully knock the slot background out to transparent, then crop tight to the
    icon that's left.

    The background colour is estimated from the *whole border ring* (its median,
    so a stray bright pixel can't throw it off), and every pixel close to that
    colour anywhere in the image is made transparent — not just the ones touching
    an edge — so interior patches of background go too. Near-black slot shading is
    also dropped. What survives is cropped to its bounding box. Best-effort: on a
    busy or gradient background it won't be pixel-perfect."""
    from PIL import Image

    rgba = img.convert("RGBA")
    if max(rgba.size) > 160:
        rgba.thumbnail((160, 160), Image.LANCZOS)
    px = rgba.load()
    w, h = rgba.size
    if w < 2 or h < 2:
        return rgba

    # background colour = median of the 1px border ring
    ring = []
    for x in range(w):
        ring.append(px[x, 0]); ring.append(px[x, h - 1])
    for y in range(h):
        ring.append(px[0, y]); ring.append(px[w - 1, y])
    n = len(ring)
    br = sorted(c[0] for c in ring)[n // 2]
    bg = sorted(c[1] for c in ring)[n // 2]
    bb = sorted(c[2] for c in ring)[n // 2]

    minx, miny, maxx, maxy = w, h, 0, 0
    for y in range(h):
        for x in range(w):
            r, g, b, _ = px[x, y]
            near_bg = abs(r - br) + abs(g - bg) + abs(b - bb) < tol
            dark_slot = max(r, g, b) < 50 and (max(r, g, b) - min(r, g, b)) < 22
            if near_bg or dark_slot:
                px[x, y] = (r, g, b, 0)
            else:
                if x < minx: minx = x
                if y < miny: miny = y
                if x > maxx: maxx = x
                if y > maxy: maxy = y
    if maxx <= minx or maxy <= miny:
        return rgba  # all background — leave as-is
    return rgba.crop((minx, miny, maxx + 1, maxy + 1))


def _scan_stash(data: bytes) -> list[dict]:
    """OCR an uploaded screenshot into {item, qty, icon, image} candidates.

    Words are clustered by position (not Tesseract's line grouping) so a grid
    inventory doesn't merge items across columns, and each item's icon is cropped
    from the slot with its background cut out. Best-effort — still imperfect, so
    the page shows candidates to confirm. Returns [] if OCR is unavailable."""
    try:
        import base64 as _b64
        import io as _io

        import pytesseract
        from PIL import Image, ImageOps

        orig = Image.open(_io.BytesIO(data)).convert("RGB")
        g = ImageOps.grayscale(orig)
        big = max(g.size) or 1
        scale = 2000 / big if big < 2000 else 1.0
        proc = (
            g.resize((max(int(g.width * scale), 1), max(int(g.height * scale), 1)))
            if scale != 1.0
            else g
        )
        proc = ImageOps.autocontrast(proc)
        hist = proc.histogram()
        if sum(hist[:128]) > sum(hist[128:]):
            proc = ImageOps.invert(proc)
        dd = pytesseract.image_to_data(
            proc, config="--psm 11", output_type=pytesseract.Output.DICT
        )
        words = []
        for i in range(len(dd["text"])):
            t = (dd["text"][i] or "").strip()
            if not t:
                continue
            try:
                conf = float(dd["conf"][i])
            except (TypeError, ValueError):
                conf = 0
            if conf < 35:  # drop low-confidence noise
                continue
            words.append(
                {"t": t, "x": dd["left"][i], "y": dd["top"][i],
                 "w": dd["width"][i], "h": dd["height"][i]}
            )
        if not words:
            return []
        heights = sorted(w["h"] for w in words)
        mh = heights[len(heights) // 2] or 12

        # union-find: connect words that sit next to each other (same slot/label)
        parent = list(range(len(words)))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for i in range(len(words)):
            for j in range(i + 1, len(words)):
                A, B = words[i], words[j]
                gy = max(0, max(A["y"], B["y"]) - min(A["y"] + A["h"], B["y"] + B["h"]))
                gx = max(0, max(A["x"], B["x"]) - min(A["x"] + A["w"], B["x"] + B["w"]))
                if gy < 0.6 * mh and gx < 1.3 * mh:  # close vertically AND horizontally
                    parent[find(i)] = find(j)

        clusters: dict = {}
        for i in range(len(words)):
            clusters.setdefault(find(i), []).append(words[i])

        merged: dict = {}
        for ws in clusters.values():
            ws.sort(key=lambda w: (w["y"], w["x"]))
            line = " ".join(w["t"] for w in ws).strip()
            parsed = _parse_stash_text(line)
            if not parsed:
                continue
            cand = parsed[0]
            x0 = min(w["x"] for w in ws) / scale
            y0 = min(w["y"] for w in ws) / scale
            x1 = max(w["x"] + w["w"] for w in ws) / scale
            y1 = max(w["y"] + w["h"] for w in ws) / scale
            # The item image sits in the slot ABOVE its label, so crop the square
            # directly above the text cluster (never the text itself) and cut the
            # background out of it. Falls back to a slot-sized square if the icon
            # zone would fall off the top edge.
            cx = (x0 + x1) / 2
            side = max(x1 - x0, 24.0)
            gap = side * 0.10
            iy1 = y0 - gap            # bottom of icon = just above the label
            iy0 = iy1 - side          # one slot-height up
            if iy0 < 0:               # label near the top — fall back around it
                cyf = (y0 + y1) / 2
                iy0, iy1 = max(cyf - side / 2, 0), cyf + side / 2
            bx0 = max(cx - side / 2, 0)
            bx1 = min(cx + side / 2, orig.width)
            by0 = max(iy0, 0)
            by1 = min(iy1, orig.height)
            img_uri = ""
            if bx1 - bx0 > 8 and by1 - by0 > 8:
                try:
                    crop = _trim_bg(orig.crop((int(bx0), int(by0), int(bx1), int(by1))))
                    crop = crop.resize((64, 64), Image.LANCZOS)
                    buf = _io.BytesIO()
                    crop.save(buf, "PNG")
                    img_uri = "data:image/png;base64," + _b64.b64encode(buf.getvalue()).decode()
                except Exception:
                    pass
            slot = merged.setdefault(cand["item"], {"qty": 0, "image": ""})
            slot["qty"] += cand["qty"]
            if not slot["image"] and img_uri:
                slot["image"] = img_uri
        return [
            {"item": k, "qty": v["qty"], "icon": _item_icon(k), "image": v["image"]}
            for k, v in merged.items()
        ]
    except Exception as exc:
        log.info("stash scan failed: %s", exc)
        return []


# ---- wars (read-only) ------------------------------------------------------- #

def _wars_list(store: dict) -> list[dict]:
    out = []
    for w in store.get("wars", []):
        out.append(
            {
                "enemy": w.get("enemy", "?"),
                "wins": int(w.get("wins", 0)),
                "losses": int(w.get("losses", 0)),
                "active": bool(w.get("active", True)),
                "first_to": w.get("first_to"),
            }
        )
    out.sort(key=lambda x: (not x["active"], -(x["wins"] + x["losses"])))
    return out


# ---- EOIs (read-only, pending only) ----------------------------------------- #

def _eois_list(guild: "discord.Guild", store: dict) -> list[dict]:
    """Applications still open for voting (ended ones are skipped), soonest to
    close first."""
    apps = store.get("eoi_apps", {})
    timers = store.get("eoi_timers", {})
    votes = store.get("eoi_votes", {})
    out = []
    for mid, app in apps.items():
        t = timers.get(str(mid), {})
        if t.get("done"):  # voting has ended — hide it
            continue
        v = votes.get(str(mid), {})
        uid = app.get("user_id")
        m = guild.get_member(uid) if uid else None
        out.append(
            {
                "id": str(mid),
                "ign": app.get("ign", ""),
                "role": app.get("role", ""),
                "applicant": m.display_name if m is not None else (app.get("ign") or str(uid)),
                "avatar": str(m.display_avatar.url) if m is not None else "",
                "yes": sum(1 for x in v.values() if x == "yes"),
                "no": sum(1 for x in v.values() if x == "no"),
                "ends": t.get("ends"),
            }
        )
    out.sort(key=lambda x: (x.get("ends") is None, x.get("ends") or 0))
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
            "ranks": _rank_options(guild, store),
            "rank_order": _RANK_ORDER,
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


async def _h_stash(request):
    guild = _guild_from(request.query.get("guild"))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    items = _stash_items(store)
    return _aioweb.json_response({"items": items, "total": sum(i["qty"] for i in items)})


async def _h_stash_set(request):
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
    store = guild_store(DATA, guild.id)
    result = _stash_upsert(
        store, body.get("item"), body.get("qty"), body.get("mode", "set"),
        body.get("weight"), body.get("image"),
    )
    return _aioweb.json_response(result, status=200 if result.get("ok") else 400)


async def _h_stash_remove(request):
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
    store = guild_store(DATA, guild.id)
    return _aioweb.json_response(_stash_remove(store, body.get("item")))


async def _h_stash_scan(request):
    guild = _guild_from(request.query.get("guild"))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    try:
        post = await request.post()
    except Exception:
        return _aioweb.json_response({"error": "Bad upload."}, status=400)
    field = post.get("image")
    data = None
    if field is not None:
        data = field.file.read() if hasattr(field, "file") else (
            bytes(field) if isinstance(field, (bytes, bytearray)) else None
        )
    if not data:
        return _aioweb.json_response({"error": "No image uploaded."}, status=400)
    if len(data) > 12 * 1024 * 1024:
        return _aioweb.json_response({"error": "Image too big (max 12 MB)."}, status=400)
    if not _ocr_available():
        return _aioweb.json_response(
            {"candidates": [], "ocr": False,
             "note": "The OCR engine (Tesseract) isn't installed on the server yet — "
                     "the next deploy adds it. You can still add items by hand."}
        )
    cands = _scan_stash(data)
    if not cands:
        return _aioweb.json_response(
            {"candidates": [], "ocr": True,
             "note": "Couldn't read items from that screenshot — try a bigger/clearer "
                     "crop of just the item list."}
        )
    return _aioweb.json_response({"candidates": cands, "ocr": True})


async def _h_wars(request):
    guild = _guild_from(request.query.get("guild"))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    return _aioweb.json_response({"wars": _wars_list(store)})


async def _h_eois(request):
    guild = _guild_from(request.query.get("guild"))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    return _aioweb.json_response({"eois": _eois_list(guild, store)})


async def _web_end_eoi(request, force: str | None):
    """Shared force-end for the web: tally+close (force=None), force-accept
    (force='accept') or force-deny (force='deny'). Always drops the EOI off the
    pending list, falling back to the configured EOI channel for older apps."""
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
    mid = str(body.get("msg_id") or "")
    store = guild_store(DATA, guild.id)
    if mid not in store.get("eoi_apps", {}):
        return _aioweb.json_response({"error": "That application no longer exists."}, status=404)
    rec = dict(store.get("eoi_timers", {}).get(mid) or {})
    if not rec.get("channel_id"):
        rec["channel_id"] = store.get("eoi_channel_id")
    timer = store.setdefault("eoi_timers", {}).setdefault(mid, {})
    timer["done"] = True
    if rec.get("channel_id"):
        timer["channel_id"] = rec["channel_id"]
    save_data(DATA)
    try:
        await _close_eoi(guild, store, int(mid), rec, force=force)
    except (discord.HTTPException, ValueError) as exc:
        log.info("web EOI end (force=%s): couldn't edit the post (%s)", force, exc)
    return _aioweb.json_response({"ok": True})


async def _h_eoi_close(request):
    return await _web_end_eoi(request, None)


async def _h_eoi_accept(request):
    return await _web_end_eoi(request, "accept")


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
            _aioweb.get("/api/stash", _h_stash),
            _aioweb.post("/api/stash", _h_stash_set),
            _aioweb.post("/api/stash_remove", _h_stash_remove),
            _aioweb.post("/api/stash_scan", _h_stash_scan),
            _aioweb.get("/api/wars", _h_wars),
            _aioweb.get("/api/eois", _h_eois),
            _aioweb.post("/api/eoi_close", _h_eoi_close),
            _aioweb.post("/api/eoi_accept", _h_eoi_accept),
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
  input[type=text],input[type=number],input:not([type]),select{font:inherit;background:rgba(255,255,255,.04);
    border:1px solid var(--line);color:var(--text);padding:11px 14px;border-radius:12px;outline:none;transition:.16s}
  .cand input,.addbar input{padding:9px 12px}
  input:focus,select:focus{border-color:rgba(244,63,94,.55);background:rgba(244,63,94,.06)}
  .muted{color:var(--mute)}

  .gate{max-width:460px;margin:12vh auto;text-align:center;padding:40px 32px;border-radius:26px;
    background:linear-gradient(180deg,rgba(32,20,27,.88),rgba(18,18,24,.82));border:1px solid var(--line);
    backdrop-filter:blur(18px);box-shadow:0 30px 90px rgba(0,0,0,.6)}
  .gate .gicon{width:76px;height:76px;margin:0 auto 20px;border-radius:22px;display:grid;place-items:center;font-size:38px;
    background:linear-gradient(135deg,#2a0b16,#4c0f22);border:1px solid rgba(244,63,94,.45);box-shadow:0 0 40px rgba(225,29,72,.5)}
  .gate h2{margin:0 0 8px;font-size:23px;font-weight:800}
  .gate p{margin:0;color:var(--mute);line-height:1.6}
  #gateActions{margin-top:24px}
  #gateActions button{width:100%;padding:13px;font-size:15px}
  .discordbtn{display:inline-flex;align-items:center;justify-content:center;gap:10px;width:100%;padding:13px;
    background:#5865f2;border:1px solid #5865f2;border-radius:13px;color:#fff;font-weight:700;font-size:15px;cursor:pointer;
    box-shadow:0 12px 34px rgba(88,101,242,.45);transition:.16s}
  .discordbtn:hover{filter:brightness(1.08);transform:translateY(-1px)}

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
  #toasts{position:fixed;top:78px;right:14px;z-index:60;display:flex;flex-direction:column;gap:8px;max-width:360px}
  .toast{padding:12px 15px;border-radius:13px;font-size:14px;font-weight:500;color:#fff;
    background:rgba(20,20,26,.92);border:1px solid var(--line);backdrop-filter:blur(10px);
    box-shadow:0 10px 30px rgba(0,0,0,.5);animation:toastin .25s ease}
  .toast.ok{border-color:rgba(34,197,94,.45);background:rgba(16,40,24,.92)}
  .toast.err{border-color:rgba(239,68,68,.5);background:rgba(42,16,16,.92)}
  @keyframes toastin{from{opacity:0;transform:translateX(20px)}to{opacity:1;transform:none}}
  @media(max-width:560px){#toasts{left:14px;right:14px;max-width:none}}
  footer{color:var(--mute);font-size:12px;text-align:center;padding:30px 18px}

  .addbar{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
  .addbar .i{flex:1;min-width:150px}.addbar .q{width:92px}
  .iconbox{width:42px;height:42px;flex:none;border-radius:11px;display:grid;place-items:center;font-size:21px;
    background:rgba(255,255,255,.05);border:1px solid var(--line)}
  .iconimg{width:42px;height:42px;flex:none;border-radius:11px;object-fit:cover;border:1px solid var(--line);
    background:#000;image-rendering:auto}
  .wchip{display:inline-flex;align-items:center;gap:5px;font-size:11px;color:var(--mute)}
  .wi{width:60px;padding:3px 7px !important;border-radius:8px;font-size:11px;text-align:center}
  .cand .thumb{width:36px;height:36px;flex:none;border-radius:8px;object-fit:cover;border:1px solid var(--line);background:#000}
  .cand .temoji{width:36px;height:36px;flex:none;border-radius:8px;display:grid;place-items:center;
    background:rgba(255,255,255,.05);border:1px solid var(--line);font-size:18px}
  .drop{border:2px dashed var(--line);border-radius:16px;padding:22px;text-align:center;cursor:pointer;
    transition:.18s;background:rgba(255,255,255,.02);margin-bottom:14px}
  .drop:hover,.drop.over{border-color:rgba(244,63,94,.6);background:rgba(244,63,94,.07)}
  .drop b{font-size:15px}
  .cand{display:flex;gap:8px;align-items:center;padding:7px 0;border-bottom:1px solid var(--line)}
  .cand .ci{flex:1}.cand .cq{width:78px}
  .sitem{display:flex;align-items:center;gap:12px;background:var(--card);border:1px solid var(--line);
    border-radius:13px;padding:10px 14px;backdrop-filter:blur(8px)}
  .sitem .q{display:flex;align-items:center;gap:7px}
  .sitem .q button{padding:4px 11px;border-radius:9px;font-weight:800}
  .sitem .qn{min-width:46px;text-align:center;font-weight:800;font-variant-numeric:tabular-nums}
  .warcard{display:flex;align-items:center;gap:14px;background:var(--card);border:1px solid var(--line);
    border-radius:14px;padding:14px 16px;backdrop-filter:blur(8px)}
  .warcard .score{font-size:23px;font-weight:800;font-variant-numeric:tabular-nums}
  .badge{font-size:11px;font-weight:700;padding:2px 10px;border-radius:999px;text-transform:uppercase;letter-spacing:.5px}
  .badge.live{background:rgba(34,197,94,.15);color:#86efac;border:1px solid rgba(34,197,94,.4)}
  .badge.done{background:rgba(255,255,255,.05);color:var(--mute);border:1px solid var(--line)}
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
    <div class="tab" data-tab="stash" onclick="setTab('stash')">📦 Stash</div>
    <div class="tab" data-tab="wars" onclick="setTab('wars')">⚔️ Wars</div>
    <div class="tab" data-tab="eois" onclick="setTab('eois')">📨 EOIs</div>
  </div>

  <div class="toolbar">
    <input id="search" type="text" placeholder="Search members…" oninput="render()">
    <button onclick="reload()">↻ Refresh</button>
    <span id="count" class="count"></span>
  </div>

  <div id="rosterView"></div>
  <div id="kosView" style="display:none"></div>
  <div id="stashView" style="display:none"></div>
  <div id="warsView" style="display:none"></div>
  <div id="eoisView" style="display:none"></div>
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
const DSVG='<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" style="flex:none"><path d="M20.3 4.4A19.8 19.8 0 0 0 15.4 3l-.2.5c1.7.4 3.1 1 4.3 1.6a13 13 0 0 0-11 0C9.6 4.4 11 3.9 12.6 3.5L12.4 3a19.8 19.8 0 0 0-5 1.4C4 8.3 3.2 12.1 3.5 15.8a19.9 19.9 0 0 0 6 3l.8-1.1c-.7-.3-1.4-.6-2-1l.5-.4a14.2 14.2 0 0 0 12 0l.5.4c-.6.4-1.3.7-2 1l.8 1.1a19.9 19.9 0 0 0 6-3c.4-4.3-.7-8.1-3.5-11.4ZM9.3 13.8c-1 0-1.8-.9-1.8-2s.8-2 1.8-2 1.8.9 1.8 2-.8 2-1.8 2Zm5.4 0c-1 0-1.8-.9-1.8-2s.8-2 1.8-2 1.8.9 1.8 2-.8 2-1.8 2Z"/></svg>';
function gate(icon,title,text,actions){
  show("app",false);show("gate",true);$("logoutBtn").style.display="none";
  if(window.__recheck){clearInterval(window.__recheck);window.__recheck=null;}
  $("gate").innerHTML=`<div class="gicon">${icon}</div><h2>${h(title)}</h2><p class="muted">${h(text)}</p><div id="gateActions">${actions||""}</div>`;
}
async function boot(){
  let me;try{me=await api("/api/me");}catch(e){gate("⚠️","Can't connect","Couldn't reach the server — try again.","<button onclick='boot()'>Retry</button>");return;}
  if(!me.authed){
    if(me.reason==="not_configured") gate("⚙️","Setup needed","The owner still needs to set DISCORD_CLIENT_SECRET and a Website access role before anyone can log in.");
    else if(me.reason==="no_access") gate("🚫","No access","You don't have the website-access role yet. Ask leadership to give it to you, then reload this page.",'<a href="/logout"><button>Log out</button></a>');
    else gate("🐉","Tenshi Roster","Log in with Discord to manage the roster, stash, KOS and more.",'<a href="/login"><button class="discordbtn">'+DSVG+' Log in with Discord</button></a>');
    return;
  }
  show("gate",false);show("app",true);$("logoutBtn").style.display="";
  if(window.__recheck)clearInterval(window.__recheck);
  window.__recheck=setInterval(async()=>{try{const m=await api("/api/me");if(!m.authed){clearInterval(window.__recheck);window.__recheck=null;boot();}}catch(e){}},45000);
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
    const [ov,ro,ko,stn,wa,eo]=await Promise.all([api("/api/overview?guild="+g),api("/api/roster?guild="+g),api("/api/kos?guild="+g),api("/api/stash?guild="+g),api("/api/wars?guild="+g),api("/api/eois?guild="+g)]);
    $("sub").textContent=ro.guild?.name||"Roster";
    renderStats(ov.overview);
    STATE.members=ro.members||[];STATE.ranks=ro.ranks||[];STATE.kos=ko.members||[];STATE.rank_order=ro.rank_order||[];STATE.stash=stn.items||[];STATE.stashTotal=stn.total||0;STATE.wars=wa.wars||[];STATE.eois=eo.eois||[];
    render();
  }catch(e){ if(e.status===401||e.status===403) return boot(); toast(e.message||"Failed to load.","err"); }
}
function countUp(el,to){
  to=Number(to); if(isNaN(to)){el.textContent=arguments[1];return;}
  const dur=600,t0=performance.now();
  (function step(t){const p=Math.min((t-t0)/dur,1);el.textContent=Math.round(to*(1-Math.pow(1-p,3)));if(p<1)requestAnimationFrame(step);})(t0);
}
function renderStats(o){
  const tiles=[["ranked","Ranked","🎯","brand"],["kos","On KOS","💀","bad"],["open_eois","Pending EOIs","📨","warn"],
    ["strikes_30d","Strikes 30d","⚠️","warn"],["wars","Wars","⚔️","blue"],["members","In server","👥","ok"]];
  $("stats").innerHTML=tiles.map(([k,l,ic,c],i)=>`<div class="stat ${c}" style="animation-delay:${i*45}ms"><span class="ic">${ic}</span><div class="n" data-c="${h(String(o[k]??'—'))}">0</div><div class="l">${l}</div></div>`).join("")
    +`<div class="stat purple" style="animation-delay:270ms"><span class="ic">📻</span><div class="n" style="font-size:17px">${h(o.radio||'—')}</div><div class="l">Radio</div></div>`;
  document.querySelectorAll('.stat .n[data-c]').forEach(e=>countUp(e,e.dataset.c));
}
function setTab(t){
  TAB=t;document.querySelectorAll(".tab").forEach(e=>e.classList.toggle("active",e.dataset.tab===t));
  ["roster","kos","stash","wars","eois"].forEach(v=>show(v+"View",t===v));
  $("search").placeholder={roster:"Search members…",kos:"Search KOS…",stash:"Search items…",wars:"Wars",eois:"Pending applications"}[t]||"Search…";render();
}
function render(){({roster:renderRoster,kos:renderKos,stash:renderStash,wars:renderWars,eois:renderEois}[TAB]||renderRoster)();}
function renderRoster(){
  const q=$("search").value.toLowerCase();
  let ms=STATE.members.filter(m=>m.name.toLowerCase().includes(q)||(m.rank||"").toLowerCase().includes(q));
  $("count").textContent=STATE.members.length+" member"+(STATE.members.length===1?"":"s");
  if(!ms.length){$("rosterView").innerHTML='<div class="empty">No members match.</div>';return;}
  const groups={};ms.forEach(m=>{const k=m.rank||"Unranked";(groups[k]=groups[k]||[]).push(m);});
  const order=STATE.rank_order||[];
  const ri=n=>{const i=order.indexOf((n||"").toLowerCase());return i<0?order.length:i;};
  const names=Object.keys(groups).sort((a,b)=>{
    if(a==="Unranked")return 1;if(b==="Unranked")return -1;
    const d=ri(a)-ri(b);if(d)return d;
    return groups[b].length-groups[a].length||a.localeCompare(b);
  });
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

function renderStash(){
  const q=$("search").value.toLowerCase();
  const all=STATE.stash||[];
  const items=all.filter(i=>i.item.toLowerCase().includes(q));
  const totQ=all.reduce((a,i)=>a+(i.qty||0),0);
  const totW=all.reduce((a,i)=>a+(i.qty||0)*(i.weight||0),0);
  $("count").textContent=all.length+" items · "+totQ+" total"+(totW>0?" · "+(Math.round(totW*10)/10)+" kg":"");
  const add=`<div class="addbar"><input class="i" id="si" placeholder="Item name" onkeydown="if(event.key==='Enter')stashAdd()"><input class="q" id="sq" type="number" placeholder="Qty" value="1"><input class="q" id="sw" type="number" step="0.1" placeholder="kg ea"><button class="brand-btn" onclick="stashAdd()">Add</button></div>`;
  const drop=`<div class="drop" id="drop" onclick="$('file').click()"><div>📸 <b>Drop a stash screenshot</b> or click to upload</div><small class="muted">Reads item names + counts with OCR (weight bar ignored) — you confirm before anything is added</small><input id="file" type="file" accept="image/*" hidden></div><div id="cands" class="card" style="display:none;padding:14px;margin-bottom:14px"></div>`;
  const list=items.length?`<ul class="members" style="gap:8px">`+items.map(stashRow).join("")+`</ul>`:`<div class="empty">Stash is empty — add an item or scan a screenshot.</div>`;
  $("stashView").innerHTML=add+drop+list;wireDrop();
}
function stashRow(i){
  return `<li class="sitem">${i.image?`<img class="iconimg" src="${i.image}" alt="">`:`<div class="iconbox">${i.icon||"📦"}</div>`}<div class="grow"><div class="mname">${h(i.item)}</div>
    <div class="chips"><span class="wchip">⚖ <input class="wi" type="number" step="0.1" value="${i.weight||0}" title="Weight each (kg)" onchange="stashWeight('${h(i.item)}',this.value)"> kg ea${i.weight>0?` · ${Math.round(i.qty*i.weight*10)/10} kg total`:""}</span></div></div>
    <div class="q"><button onclick="stashDelta('${h(i.item)}',-1)">−</button><span class="qn">${i.qty}</span><button onclick="stashDelta('${h(i.item)}',1)">+</button></div>
    <button class="danger" onclick="stashRemove('${h(i.item)}')">✕</button></li>`;
}
async function stashWeight(item,w){try{await post("/api/stash",{guild:STATE.guild,item,qty:0,mode:"add",weight:w});await reload();}catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}}
function wireDrop(){const d=$("drop"),f=$("file");if(!d)return;f.onchange=()=>{if(f.files[0])scanFile(f.files[0]);};d.ondragover=e=>{e.preventDefault();d.classList.add("over");};d.ondragleave=()=>d.classList.remove("over");d.ondrop=e=>{e.preventDefault();d.classList.remove("over");if(e.dataTransfer.files[0])scanFile(e.dataTransfer.files[0]);};}
async function scanFile(file){toast("Scanning screenshot…");const fd=new FormData();fd.append("image",file);
  try{const r=await fetch("/api/stash_scan?guild="+encodeURIComponent(STATE.guild),{method:"POST",body:fd});const d=await r.json();
    if(!r.ok)throw new Error(d.error||"Scan failed");
    if(!d.candidates||!d.candidates.length){toast(d.note||"Couldn't read any items from that image.","err");return;}
    renderCandidates(d.candidates);}catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Scan failed","err");}}
function renderCandidates(c){const el=$("cands");el.style.display="";window.__cimg=c.map(x=>x.image||"");
  el.innerHTML=`<div style="font-weight:700;margin-bottom:10px">Found ${c.length} — tick what to add, fix names/qty/weight, then confirm</div>`
    +c.map((x,i)=>`<div class="cand"><input type="checkbox" checked id="c${i}">${x.image?`<img class="thumb" src="${x.image}">`:`<div class="temoji">${x.icon||"📦"}</div>`}<input class="ci" id="ci${i}" value="${h(x.item)}" title="Item"><input class="cq" id="cq${i}" type="number" value="${x.qty}" title="Qty"><input class="cq" id="cw${i}" type="number" step="0.1" placeholder="kg" title="Weight each"></div>`).join("")
    +`<div style="margin-top:12px;display:flex;gap:8px"><button class="brand-btn" onclick="addCandidates(${c.length})">Add to stash</button><button onclick="$('cands').style.display='none'">Cancel</button></div>`;}
async function addCandidates(n){let added=0;
  for(let i=0;i<n;i++){const cb=$("c"+i);if(!cb||!cb.checked)continue;const item=$("ci"+i).value.trim(),qty=$("cq"+i).value;if(!item)continue;
    const weight=($("cw"+i)||{}).value||"";const image=(window.__cimg||[])[i]||"";
    try{await post("/api/stash",{guild:STATE.guild,item,qty,weight,image,mode:"add"});added++;}catch(e){}}
  const el=$("cands");if(el)el.style.display="none";toast("Added "+added+" item"+(added===1?"":"s")+" to the stash","ok");await reload();}
async function stashAdd(){const item=($("si")||{}).value?.trim(),qty=($("sq")||{}).value||1,weight=($("sw")||{}).value||"";
  if(!item){toast("Enter an item name","err");return;}
  try{await post("/api/stash",{guild:STATE.guild,item,qty,weight,mode:"add"});toast("Added "+item,"ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}}
async function stashDelta(item,d){try{await post("/api/stash",{guild:STATE.guild,item,qty:d,mode:"add"});await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}}
async function stashRemove(item){try{await post("/api/stash_remove",{guild:STATE.guild,item});toast("Removed "+item,"ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}}
function renderWars(){const ws=STATE.wars||[];$("count").textContent=ws.length+" war"+(ws.length===1?"":"s");
  if(!ws.length){$("warsView").innerHTML=`<div class="empty">No wars tracked.</div>`;return;}
  $("warsView").innerHTML=`<ul class="members" style="gap:9px">`+ws.map(w=>{
    const col=w.wins>w.losses?"var(--ok)":w.losses>w.wins?"var(--bad)":"var(--mute)";
    return `<li class="warcard"><div class="grow"><div class="mname">vs ${h(w.enemy)}</div><div class="muted" style="font-size:12px">${w.active?"In progress":"Ended"}${w.first_to?" · first to "+w.first_to:""}</div></div><span class="badge ${w.active?'live':'done'}">${w.active?'Live':'Done'}</span><div class="score" style="color:${col}">${w.wins} – ${w.losses}</div></li>`;}).join("")+`</ul>`;}
function renderEois(){const es=STATE.eois||[];$("count").textContent=es.length+" pending";
  if(!es.length){$("eoisView").innerHTML=`<div class="empty">No pending applications. 🎉</div>`;return;}
  $("eoisView").innerHTML=`<ul class="members" style="gap:9px">`+es.map(e=>`<li class="member">${e.avatar?`<div class="av"><img src="${h(e.avatar)}" alt=""></div>`:`<div class="av skull" style="background:linear-gradient(135deg,#1a2033,#243154)">📨</div>`}<div class="grow"><div class="mname">${h(e.ign||e.applicant||"Applicant")}</div><div class="muted" style="font-size:12px">${h(e.role||"—")}${e.applicant&&e.applicant!==e.ign?" · "+h(e.applicant):""}</div></div><div style="display:flex;gap:7px;align-items:center">${e.ends?`<span class="badge done" data-ts="${e.ends}"></span>`:""}<span class="badge" style="background:rgba(34,197,94,.15);color:#86efac;border:1px solid rgba(34,197,94,.4)">✅ ${e.yes}</span><span class="badge" style="background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid rgba(239,68,68,.4)">❌ ${e.no}</span><button style="padding:6px 11px;background:linear-gradient(135deg,#16a34a,#15803d);border:none;color:#fff;font-weight:700;border-radius:11px;cursor:pointer" onclick="acceptEoi('${h(e.id)}','${h(e.ign||e.applicant||"this")}')">Accept</button><button class="danger" style="padding:6px 11px" onclick="closeEoi('${h(e.id)}','${h(e.ign||e.applicant||"this")}')">Close</button></div></li>`).join("")+`</ul>`;
  document.querySelectorAll('#eoisView [data-ts]').forEach(el=>{const t=+el.dataset.ts,sec=t-Math.floor(Date.now()/1000);el.textContent=sec<=0?"closing":"closes "+(sec>86400?Math.round(sec/86400)+"d":sec>3600?Math.round(sec/3600)+"h":Math.max(Math.round(sec/60),1)+"m");});
}
async function closeEoi(id,name){
  if(!confirm("Close voting for "+name+"'s application now? Votes are tallied and the post locks."))return;
  try{await post("/api/eoi_close",{guild:STATE.guild,msg_id:id});toast("Closed "+name+"'s EOI","ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
async function acceptEoi(id,name){
  if(!confirm("Force-accept "+name+"? They'll be welcomed and given the roster role regardless of the votes."))return;
  try{await post("/api/eoi_accept",{guild:STATE.guild,msg_id:id});toast("Accepted "+name,"ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
boot();
</script>
</body>
</html>
"""


# export for the entrypoint (start_web_server) via star import
__all__ = [n for n in list(globals()) if not n.startswith("__")]
