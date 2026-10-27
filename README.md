# FiveM Gang Management Bot

A Discord bot to manage a FiveM roleplay gang: roster, Kill-On-Sight list, and an
Expression-of-Interest (EOI) application system with Yes/No voting.

## Commands

All setup lives under `/config`. Staff commands need the leadership role or admin.

### Roster
| Command | What it does | Who |
|---|---|---|
| `/roster view` | Show the roster | Everyone |
| `/roster add @member [+4 more] [rank]` | Add up to 5 members; a picked rank gives that rank's role | Everyone |
| `/roster remove @member` | Remove a member and take the role back | Everyone |
| `/roster reset` | Wipe the roster list (keeps everyone's role) | Leadership |
| `/roster rank_set <name> <role>` | Map a rank name to a server role | Leadership |
| `/roster rank_remove <name>` / `rank_list` | Remove / list ranks | Leadership / Everyone |

### KOS
| Command | What it does | Who |
|---|---|---|
| `/kos view` | Show the KOS list | Everyone |
| `/kos add @member [+4 more] [reason]` | Add up to 5 to KOS and give the KOS role | Everyone |
| `/kos remove @member` | Remove and take the role back | Everyone |
| `/kos reset` | Wipe the list (keeps roles) | Leadership |

### Tickets / Orders
| Command | What it does | Who |
|---|---|---|
| `/ticket type_add <name> [category] [emoji]` | Add a ticket type (Gang Orders, Vendors, Gun Crafting…) + its channel category | Leadership |
| `/ticket type_remove <name>` / `type_list` | Remove / list types | Leadership / Everyone |
| `/ticket panel [message]` | Post a dropdown menu of types; picking one opens a private ticket | Leadership |
| `/ticket board` | Active orders grouped by type, paid/unpaid | Staff |
| `/ticket close` | Close the ticket you're in (saves transcript, deletes channel) | Staff |
| (in ticket) 💰 Mark Paid · 🔒 Close | Mark paid, or close → transcript then delete | Staff |

### Recruitment · Radio · Roles
| Command | What it does | Who |
|---|---|---|
| `/eoi` | Open the application form; posts to the EOI channel with Yes/No votes | Everyone |
| `/eoi_post [message]` | Post an "Apply" button | Leadership |
| `/radio new` / `view` / `panel` | Roll a new frequency / show it / post a reroll button | Leadership / Everyone |
| `/role_menu add` / `remove` / `list` / `post` | Manage & post a self-serve role dropdown | Leadership |

### Setup (`/config`)
| Command | What it does | Who |
|---|---|---|
| `/config set <setting> [channel] [role]` | Set/clear any channel or role: leadership, log, EOI, transcript, roster, KOS, radio-ping | Leadership (leadership role = admin only) |
| `/config view` | Show all current settings | Leadership |
| `/config log_setup` | One-click: create a private `#gang-logs` channel and start logging | Leadership |
| `/gang_help` | List all commands | Everyone |

## Setup

1. Create a bot at <https://discord.com/developers/applications>, copy its **token**.
2. Under **Bot**, no privileged intents are required (everything uses slash commands).
3. Invite it with the `applications.commands` and `bot` scopes, and permission to
   send messages / embed links in the channels you care about.
4. For the roster role feature, give the bot **Manage Roles**, and drag the bot's
   own role **above** the roster role in Server Settings → Roles (Discord only lets
   a bot manage roles beneath its highest one). Then run `/roster set_role`.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DISCORD_TOKEN` | yes | — | Your bot token |
| `LEADERSHIP_ROLE` | no | `Leadership` | Name of the role allowed to run leadership actions |
| `DATA_FILE` | no | `gang_data.json` | Where data is saved |

## Run locally

```bash
pip install -r requirements.txt
export DISCORD_TOKEN="your-token-here"
python gang_bot.py
```

## Run on Railway

Same as your other bot: push this repo, add `DISCORD_TOKEN` (and optionally
`LEADERSHIP_ROLE`) as service variables, and set the start command to
`python gang_bot.py`.

> **Data persistence note:** Railway's filesystem resets on redeploy. To keep the
> roster/KOS between deploys, attach a Volume and point `DATA_FILE` at it, e.g.
> `DATA_FILE=/data/gang_data.json`.
