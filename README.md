# AIAVBOT – Setup

AIAVBOT replies to Suno links in the music channel and offers members quick, optional ways to share more:
add a line about the song, post to Collab Requests, or start a thread. Design: `../AIAVBOT_Suno_Flow_Spec.md`.

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
/aiav setup music:#your-music-channel collab:#your-collab-channel gallery:#your-gallery lounge:#your-lounge
/aiav status
```

`/aiav status` warns if the bot is missing any channel permissions and checks that Suno is reachable.

Optional:

```
/aiav theme add name:Spooky Season where:#spooky-songs emoji:🎃 description:Halloween vibes ends:2026-10-31
/aiav theme list
/aiav theme end name:Spooky Season
/aiav settings reply_timeout:15
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
| Collab requests | anything new: an idea, image, Suno or other link | asks **🤝 Starting a collab?** Yes opens the collab form. The prompt becomes the request card with **🙋 I'm interested** and its own thread. **No** removes the prompt and privately, politely reminds them the channel is for collab requests, and that a thread or the lounge suits other posts. **Characters** (a Stoop/Chub/JanitorAI/... link or a PNG/JSON character card file) get *"🎭 Looks like a character!"* and character collab types: theme song, new art, animation, voice, story, roleplay, worldbuilding. |
| Gallery | anything new | asks **✨ Is this a collab result?** Yes asks who they worked with (server members and/or anyone else by name), then posts a congratulations card with a comment thread. **No** removes the prompt and privately, politely reminds them the gallery is for collab results, and that a thread, the lounge or the music channel suits other work. |
| Lounge | (nothing) | posts the **🌙 nightly update** at midnight with activity for the last day, 7 days and 30 days |
| All of the above + theme channels | reactions on a post with a link, image, file or embed | counts them (shown in the nightly update) and cheers milestones: at **3, 10, 20, 30, 50** reactions it replies to the post, e.g. *"✨ Wow, … just hit 10 reactions! Go @Creator!"* (no ping) |

In the collab and gallery channels the bot ignores replies, messages inside threads, and short remarks
like "cool!" (under 15 characters with no link or image). Only the poster can use their prompt's buttons,
and unused prompts disappear after the reply timeout.

Every collab request, whether from the music panel or the collab channel, gets its own thread.
**I'm interested** pings the person into that thread with the creator.

**Reaction milestones:** each milestone is announced once per post. Reactions on the bot's theme copies,
collab cards and gallery cards count for the member who made them. `/aiav settings reaction_milestones:False`
turns the shout-outs off (reactions are still counted). `milestone_channels:Every channel` extends them to the
whole server. Milestone numbers and messages are in `texts.py` (`MILESTONES`, `MILESTONE_MESSAGES`).

**Nightly update:** posted at midnight on the PC's clock. If the PC was off or asleep, it's posted when
the bot starts, as long as it's before noon. `/aiav update` posts one right now.
`/aiav settings nightly_update:False` turns it off.

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
| `nightly.py` | The midnight lounge update |
| `reactions.py` | Reaction counting and milestone shout-outs |
| `storage.py` | SQLite database |
| `suno_fetch.py` | Reads song info from Suno (also runs alone: `python suno_fetch.py <link>`) |
| `character_links.py` | Recognises character links (The Stoop, Chub, JanitorAI, ...) and reads PNG/JSON character card files. Add sites in `CHARACTER_SITES`. |
| `music_links.py` | Recognises other music sites and reads their title / artist / cover. Add sites in `MUSIC_SITES`. |
| `texts.py` | All wording |
| `run_bot.bat` | Windows launcher |

## Security

- Never commit `.env`. It holds the bot token. `.gitignore` already excludes it, and `.env.example` is the blank template.
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
