<p align="center">
  <img src="AIAVBot_Icon.png" alt="AIAVBOT" width="220">
</p>

<h1 align="center">AIAVBOT</h1>

<p align="center">
  <em>A Discord bot that turns quick shares into conversation, discovery and collaboration<br>
  for the <strong>Dreamers AI Hub</strong> community.</em>
</p>

<p align="center">
  🎵 Music sharing · 🤝 Collab requests · 🎭 Characters · 🎉 Gallery · 🏆 Reaction milestones · 📅 Weekly updates
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg" alt="License: AGPL-3.0-or-later"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/discord.py-2.6%2B-5865F2.svg" alt="discord.py 2.6+">
</p>

AIAVBOT keeps sharing easy and makes it more rewarding. When members post music, characters, collab ideas or
finished work, the bot replies with quick, optional ways to go further: add a line in their own words,
start a conversation, find collaborators, join a seasonal theme, or celebrate a finished collab. It's
product-agnostic: Suno, YouTube, Spotify, The Stoop, Chub, character card files and more all work.

# Setup

## 1. Create the bot in Discord (one time)

1. Go to <https://discord.com/developers/applications> → **New Application** → name it (e.g. AIAVBOT).
2. **Bot** tab:
   - **Reset Token** → copy the token (you'll paste it into `.env`). Never share it.
   - Under **Privileged Gateway Intents**, turn on **Message Content Intent**. Save.
   - Optional: turn off **Public Bot** so only you can invite it.
3. Invite it to your **test server**. Replace `APP_ID` with the Application ID from the **General Information** tab:

   ```
   https://discord.com/oauth2/authorize?client_id=APP_ID&scope=bot+applications.commands&permissions=309237763072
   ```

   That permission set covers View Channel, Send Messages, Embed Links, Attach Files, Read Message History,
   Create Public Threads and Send Messages in Threads. Nothing more.

   **Already invited the bot?** Give its role **Attach Files** (Server Settings → Roles → AIAVBot).
   It's needed to show members' images on collab and gallery cards. Without it, the cards just go without the image.

## 2. Settings file

1. Make the folder `%LOCALAPPDATA%\AIAVBOT` (paste that into the Explorer address bar; create the folder if it isn't there).
2. Copy `.env.example` there as `.env` and fill in:
   - `DISCORD_TOKEN`: the token from step 1
   - `DEV_GUILD_ID`: your test server's ID (turn on Developer Mode in Discord's Advanced settings, then right-click the server icon → **Copy Server ID**). Slash commands then show up instantly.

The bot also reads a `.env` next to `bot.py`, but keep the token out of Dropbox if this folder is shared with anyone.

## 3. Run it

Double-click **`run_bot.bat`**. The first run sets up Python packages (needs Python 3.10+ from python.org). After that it starts in a few seconds. Leave the window open while the bot should be online.

The database and a log file (`aiavbot.log`) live in `%LOCALAPPDATA%\AIAVBOT`, outside Dropbox, because syncing can corrupt an open database.

## Running it 24/7 on a VPS: test + live

The bot runs as two separate copies on one small Linux server:
- **test**: branch `dev`, its own bot, your test server
- **live**: branch `main`, its own bot, Dreamers

They share no code, settings or data. Work on `dev`, try it on the test server, then merge to `main` for live.
One script sets up each copy (service, auto-restart, daily backups, firewall, security updates):
see **[`deploy/DEPLOY.md`](deploy/DEPLOY.md)**.

## 4. Set it up in the server

`/aiav` commands are for mods and admins only. Administrators and members with Manage Server can always
use them. To let a mod role use them too (admins only):

```
/aiav modrole add role:@Moderator
/aiav modrole list
```

Regular members don't see `/aiav` at all. Anyone else who tries is politely refused. If mods can't see the
commands, their role probably lacks Manage Messages. Fix it in Server Settings → Integrations → AIAVBOT.

Then, in the test server:

```
/aiav setup music:#your-music-channel collab:#your-collab-channel gallery:#your-gallery lounge:#your-lounge showcase:#multimedia-gallery
/aiav status
```

`/aiav status` warns if the bot is missing any channel permissions and checks that Suno is reachable.

**Character porch (optional):**

```
/aiav setup porch:#the-porch porch_18:#the-porch-after-dark mod_alerts:#mod-alerts
/aiav settings stoop_feed:True stoop_feed_18:True
```

`porch_18` must be an **age-restricted** channel (channel settings → Age-Restricted Channel), or setup refuses it.
`mod_alerts` is a private mod channel for 🚩 reports and heads-ups about removed characters.
The arrival feeds need a Stoop partner key (see [The Stoop partner API](#the-stoop-partner-api-optional)).

Optional:

```
/aiav theme add name:Spooky Season where:#spooky-songs emoji:🎃 description:Halloween vibes ends:2026-10-31
/aiav theme list
/aiav theme end name:Spooky Season
/aiav settings reply_timeout:15
/aiav gallery_report                  (who was involved in last month's gallery publications)
/aiav gallery_report month:2026-09 public:True
```

**Themes:** while a theme is active, members see a **🎃 Share to Spooky Season** button on their song panel.
It posts a copy of the song (cover, style, their note and links back) to the theme's `where:`, which can be:
- a **text channel**: the copy is posted as a message
- a **thread** (or an existing forum post): the copy is posted inside it
- a **forum**: each song becomes its own new post

With several active themes, the button becomes **🏷️ Share to a theme** with a dropdown. With none, it's hidden.
If a forum requires tags, the bot can't post there. Turn that off or use a thread instead.

## What the bot does in each channel

| Channel | When someone posts... | The bot... |
|---|---|---|
| Music | a music link: Suno, YouTube, Spotify, SoundCloud, Bandcamp, Apple Music, Udio, Tidal, Deezer, Audiomack | thanks them with **🚀 Enhance Sharing** (poster only: label it, start a conversation, collab request, join a theme) and **❤️ Like on Suno/YouTube/…** buttons anyone can use. Suno links show style and lyrics. Other sites show the title, artist/channel and cover when available. After the timeout, or "Just sharing", the reply shrinks to one line but keeps its buttons. |
| Collab requests | anything new: an idea, image, Suno or other link | asks **What's this post?** with four options. **🤝 A new collab request** opens the collab form (it starts with **Name your collab**); the prompt becomes the request card with **🙋 I'm interested** and its own thread. **💬 I'm responding to a collab** shows a private list of recent requests; the bot posts their message and files in that collab's thread and pings its creator. **🎉 It's a finished collab** opens the gallery form and presents it in the gallery channel (files included). **None of these** removes the prompt and privately, politely reminds them the channel is for collab requests, and that comments belong in the collab's thread or the lounge. **Characters** (a Stoop/Chub/JanitorAI/... link or a PNG/JSON character card file) get *"🎭 Looks like a character!"* and character collab types: theme song, new art, animation, voice, story, roleplay, worldbuilding. |
| Gallery | anything new | asks **✨ Is this a collab result?** Yes asks who they worked with (server members and/or anyone else by name), then posts a congratulations card with a comment thread. Every presented collab is also **copied to the multimedia gallery** (`showcase:`), files included, with a link back to the comments. **No** removes the prompt and privately, politely reminds them the gallery is for collab results, and that a thread, the lounge or the music channel suits other work. |
| Porch (SFW) | a character: a Stoop / Chub / JanitorAI / ... link, or a PNG/JSON character card file | replies with a character card: art, summary, creator, tags and **🧵 Start a thread** (a conversation thread for the character), **💜 I'm interested in this card** (a count, plus an optional comment in the thread), **🤝 Start a collab** (the character collab form; the request goes to collab requests) and **🚩 Report** (private note to the mods; for Stoop characters also to The Stoop's moderators, without the reporter's name). With the Stoop key, the **arrival feed** also posts new SFW Stoop characters here. An 18+ character shared here shows only its name, a link and "🔞 18+ character". |
| 18+ porch (age-restricted) | the same | the same, with 18+ characters shown in full. The 18+ arrival feed posts new 18+ Stoop characters here. Anything copied out of this channel (e.g. a collab request) shows only the name, a link and 🔞. |
| Lounge | (nothing) | posts the **📅 activity update**: weekly (Monday midnight, this week + last 30 days) by default, or daily. Optionally the **📚 monthly gallery report** on the 1st. |
| All of the above + theme channels | reactions on a post with a link, image, file or embed | counts them (shown in the activity update) and cheers milestones: at **3, 10, 20, 30, 50** reactions it replies to the post, e.g. *"✨ Wow, … just hit 10 reactions! Go @Creator!"* (no ping) |

In the collab and gallery channels the bot ignores replies, messages inside threads, and short remarks
like "cool!" (under 15 characters with no link or image). Only the poster can use their prompt's buttons,
and unused prompts disappear after the reply timeout.

Every collab request, whether from the music panel or the collab channel, gets its own thread. **Whenever the bot
makes a thread, the member names it first:** the collab form ("Name your collab", which also heads the request card),
the gallery form, the music **Start a conversation** form and the porch **🧵 Start a thread** all open with a name
field, prefilled with a suggestion (the song or character, or the first line of their post) that they can keep or change.

**Gallery report:** `/aiav gallery_report` reads the gallery channel for last month (or `month:YYYY-MM`) and lists
everyone involved in a publication, most first, e.g. `Pac: 19 · Oak: 5 · Joe: 2`. A presented collab counts once for
the presenter, each collaborator they picked and each name typed under "Anyone else?". Any other post with an image,
file or link counts for its poster. Replies and chat don't count. Private by default; `public:True` posts it.
**I'm interested** pings the person into that thread with the creator.

**Reaction milestones:** each milestone is announced once per post. Reactions on the bot's theme copies,
collab cards and gallery cards count for the member who made them. `/aiav settings reaction_milestones:False`
turns the shout-outs off (reactions are still counted). `milestone_channels:Every channel` extends them to the
whole server. Milestone numbers and messages are in `texts.py` (`MILESTONES`, `MILESTONE_MESSAGES`).

**Activity update:** posted in the lounge at midnight (server clock), **weekly on Mondays** by default with
"This week" and "Last 30 days": shares, panels opened, collab requests, interested, gallery collabs and reactions
(plus a characters line when the porch is set up). `/aiav settings activity_update:` switches between weekly, daily
and off. If the bot was down at midnight, it posts when it's back, as long as it's before noon.
`/aiav update` posts one right now.

**Gallery report:** `/aiav settings gallery_report_auto:Monthly` posts last month's report (who was involved in
gallery publications, with 🥇🥈🥉 for the top three) in the lounge on the 1st of each month.

## The Stoop partner API (optional)

The Stoop (FrontPorch AI's character hub) gave AIAVBOT a **read-only partner key**. It can't vote, download,
comment, message or upload. It lets the bot:

- post an **arrival card** for every new Stoop character (SFW in the porch, 18+ in the 18+ porch)
- show full character cards for Stoop links: art, summary, tags, "by {creator} on The Stoop" (linked to their profile,
  with their hub badge), original-creator credit, score, downloads, Mod's Pick, token count and a **Download on The Stoop** button
- keep cards in sync: **edits** are applied in place (plus an "✨ updated v2 → v3" note in the character's thread),
  **removed** characters are hidden right away and tombstoned after 7 days, and characters that come back are restored
- warn mods (in `mod_alerts`) when a character shared in an SFW channel is removed or becomes 18+

Put the key in the bot's env file, never in the repo: `STOOP_API_KEY=pk_live_...` (the test bot and the live bot
each have their own key). Without a key, the porch still works with the public link previews, just without the
feed, the art or the syncing. `/aiav status` shows whether the key works, never the key itself.

**Rating safety:** the key can see 18+ characters, so the bot checks every card's rating itself. 18+ art and text
appear only in the age-restricted porch. The SFW feed asks The Stoop for SFW characters only, and checks again.

**The Stoop library (mods):** the feeds only post characters created after they're switched on. To bring in the
rest of The Stoop's catalog:

```
/aiav stoop check                                   how many Stoop characters aren't in the porch channels yet
/aiav stoop backfill channel:SFW porch per_hour:20  post them slowly, oldest first ("📚 From The Stoop's library")
/aiav stoop backfill_stop channel:SFW porch         pause (run backfill again to continue)
/aiav stoop skip card:<link>  or  creator:<profile link>   never auto-post it (removes cards already posted)
/aiav stoop unskip …  ·  /aiav stoop skipped
```

New arrivals keep posting while a backfill runs, the backfill survives restarts, and mods are told when it finishes.
🚩 reports in the mod channel also get a **🙈 Remove & skip** button. Skipping only stops *automatic* posting;
members can still share a skipped character themselves.

Details: `AIAVBOT_Stoop_Integration_Plan.md`. Code: `stoop_api.py` (API client) and `porch.py` (the porch).

## 5. Try it

1. Post a Suno link in the music channel. The bot replies right away with **🚀 Enhance Sharing** and **❤️ Like on Suno**.
2. Click it with the account that posted. The private panel opens.
3. Try each button. Also click **Enhance Sharing** from a second account to check that it politely refuses.
4. On a collab card, click **🙋 I'm interested** from the second account.
5. Post an idea in the collab channel and an image in the gallery, then click **Yes** on each prompt.
6. React to a post with a link from 3 accounts (or add 3 different emoji) to see a milestone.
7. Run `/aiav update` to see the lounge update.

## Changing wording

All member-facing text, collab types and response types are in **`texts.py`**. Edit and restart the bot.

## Files

| File | What it is |
|---|---|
| `bot.py` | Starts the bot |
| `suno_flow.py` | Music, collab-channel and gallery flows: prompts, panel, pop-ups, cards, threads |
| `admin.py` | `/aiav` admin commands |
| `nightly.py` | Scheduled lounge posts: the activity update and the monthly gallery report |
| `reactions.py` | Reaction counting and milestone shout-outs |
| `gallery_report.py` | The monthly "who was involved" count for `/aiav gallery_report` |
| `porch.py` | The character porch: character cards, thread / interested / collab / report (incl. to The Stoop's moderators), the Stoop arrival feed and card syncing |
| `stoop_api.py` | Read-only client for The Stoop partner API (paging, rate limits, images) |
| `storage.py` | SQLite database |
| `suno_fetch.py` | Reads song info from Suno (also runs alone: `python suno_fetch.py <link>`) |
| `character_links.py` | Recognises character links (The Stoop, Chub, JanitorAI, ...) and reads PNG/JSON character card files. Add sites in `CHARACTER_SITES`. |
| `music_links.py` | Recognises other music sites and reads their title / artist / cover. Add sites in `MUSIC_SITES`. |
| `texts.py` | All wording |
| `run_bot.bat` | Windows launcher |
| `run_bot_autostart.bat` | Windows launcher for the Startup folder (waits for network, restarts if stopped) |
| `deploy/` | VPS hosting for the test + live copies: setup and update scripts, systemd service template, backups, and the launch guide (`DEPLOY.md`) |

## Security

- Never commit `.env`. It holds the bot token (and the Stoop key, if you use one). `.gitignore` already excludes it, and `.env.example` is the blank template.
- If the Stoop key is ever exposed, tell The Stoop's developer so they can revoke it and issue a new one.
- If the token is ever exposed (pushed, pasted or screenshotted), reset it right away: Developer Portal → Bot → **Reset Token**.
  Then update `.env`. The old token stops working immediately.
- The database and logs live in `%LOCALAPPDATA%\AIAVBOT`, outside this folder, and are also ignored.

## Troubleshooting

- **No reply to music links:** check that Message Content Intent is on, `/aiav setup` points at the right channel, and `/aiav status` shows no missing permissions.
- **Slash commands missing:** confirm `DEV_GUILD_ID` is set to the test server, then restart. Without it, commands are registered globally, which can take a while.
- **Style and lyrics missing:** `/aiav status` shows whether Suno's data feed still works. Suno can change it without notice.
- **"Unknown interaction" (error 10062) / "didn't respond in time":** Discord allows 3 seconds to answer a click,
  and that can't be changed. The bot now acknowledges every click first and works afterwards, so this should
  be rare. Only one copy of the bot can run at a time. If the log shows *"Interaction reached the bot X s after it was made"*, sync the PC clock (Windows Settings →
  Time & language → Sync now). *"Bot was unresponsive"* means the PC was busy or asleep.
  The bot keeps its connection to Discord ready (checked every 45 s) because a cold connection was the
  main cause. *"Slow connection to Discord"* or *"Answering Discord took X s"* in the log mean the PC's
  network to Discord is slow at times (Wi-Fi, VPN, antivirus web filtering, DNS). The bot connects over IPv4
  by default. To allow IPv6, put `FORCE_IPV4=0` in `.env`.
- **Anything else:** check `%LOCALAPPDATA%\AIAVBOT\aiavbot.log`.

## License

Copyright (C) 2026 Oak

AIAVBOT is free software, licensed under the **GNU Affero General Public License v3.0 or later**
(AGPL-3.0-or-later). See [`LICENSE`](LICENSE) for the full text.

In short: anyone may use, study, change and share this bot. If you **run a modified version for other people**
(for example, on your own Discord server), the AGPL asks you to offer those users the source code of your
version. A link to your repository in the bot's Discord profile ("About Me") is an easy way to do that.

Libraries used (discord.py, aiohttp, python-dotenv) are under permissive licenses that are compatible with the AGPL.
