# FiveM Gang Management Bot

A Discord bot to manage a FiveM roleplay gang: roster, Kill-On-Sight list, and an
Expression-of-Interest (EOI) application system with Yes/No voting.

## Commands

| Command | What it does | Who |
|---|---|---|
| `/roster view` | Show the current roster | Everyone |
| `/roster set_role <role>` | Choose the role given to roster members | Leadership |
| `/roster add @member [+4 more] [rank]` | Add up to 5 members and give them the roster role | Everyone |
| `/roster remove @member` | Remove a member and take the role back | Everyone |
| `/roster reset` | Wipe the roster list (keeps everyone's role) | Leadership |
| `/kos view` | Show the KOS list | Everyone |
| `/kos set_role <role>` | Choose the role given to people added to KOS | Leadership |
| `/kos add @member [+4 more] [reason]` | Add up to 5 members to KOS and give them the KOS role | Everyone |
| `/kos remove @member` | Remove a KOS entry and take the role back | Everyone |
| `/kos reset` | Wipe the KOS list (keeps everyone's role) | Leadership |
| `/eoi` | Open the application form (Discord, Name, Montage/YouTube, Armoury, Vouches/Previous Gangs). Posts to the EOI channel with Yes/No voting buttons. | Everyone |
| `/eoi_post [message]` | Post an "Apply" button in the current channel | Leadership |
| `/eoi_channel set <channel>` | Choose where EOIs get sent | Leadership |
| `/eoi_channel show` | Show the current EOI channel | Everyone |
| `/role_menu add <role> [description]` | Add a role to the self-serve dropdown | Leadership |
| `/role_menu remove <role>` | Remove a role from the dropdown | Leadership |
| `/role_menu list` | Show the dropdown's roles | Everyone |
| `/role_menu post [title] [description]` | Post the self-serve role dropdown in this channel | Leadership |
| `/radio new` | Roll a new random radio frequency and post it in this channel | Leadership |
| `/radio view` | Show the current radio frequency | Everyone |
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
