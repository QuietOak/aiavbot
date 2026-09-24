# Deploying AIAVBOT: test + live on one VPS

Two separate copies ("instances") of the bot run on one small Linux server:

| | **test** | **live** |
|---|---|---|
| Git branch | `dev` | `main` |
| Discord bot | *AIAVBot Test* (its own application and token) | *AIAVBot* (its own application and token) |
| Discord server | your test server | Dreamers |
| Code | `/opt/aiavbot/test` | `/opt/aiavbot/live` |
| Token & server ID | `/etc/aiavbot/test.env` | `/etc/aiavbot/live.env` |
| Database, log, backups | `/var/lib/aiavbot/test/` | `/var/lib/aiavbot/live/` |
| Service | `aiavbot@test` | `aiavbot@live` |

They share nothing but the server. A broken test build can't touch live's code, settings or data.
`/aiav status` shows which instance you're talking to, e.g. `🧩 test · dev @ 4f2a91c`.

| File in `deploy/` | What it does |
|---|---|
| `setup_vps.sh` | Sets up one instance: `sudo bash setup_vps.sh test` / `live` |
| `update.sh` | Installed as `aiavbot-update`: pulls an instance's branch, backs up, restarts |
| `aiavbot@.service` | systemd template: one service per instance, starts on boot, restarts on crash |
| `aiavbot-backup` | Daily backup of every instance's database (14 days kept) |

---

## The workflow

```
  your PC                    GitHub                     VPS
  ───────                    ──────                     ───
  edit on branch dev  ──►  push dev   ──►  sudo aiavbot-update test   ──►  try it in the test server
  merge dev → main    ──►  push main  ──►  sudo aiavbot-update live   ──►  Dreamers gets it
```

On your PC:
```
git checkout dev
# ...edit, commit...
git push                       # then on the VPS: sudo aiavbot-update test

# happy with it? promote to live:
git checkout main
git merge dev
git push                       # then on the VPS: sudo aiavbot-update live
git checkout dev               # back to working on dev
```

**Hotfix on live?** Fix it on `dev` and test, then merge. For a true emergency, commit to `main`, update live,
then `git checkout dev && git merge main` so `dev` has it too.

---

## One-time preparation (on your PC)

### A. Create the `dev` branch
In the `aiavbot` folder:
```
git checkout -b dev
git push -u origin dev
```
`main` = live, `dev` = test. From now on you work on `dev`.

### B. Two Discord bots
Two copies running at the same time need **two bot applications** (one token can't run twice):
- **AIAVBot Test**: your current bot, already in the test server.
- **AIAVBot**: create a new application in the Developer Portal the same way (README step 1: turn on
  **Message Content Intent**, copy the token) and invite it to **Dreamers** with the README invite link.

### C. Stop the bot on your PC
Once the VPS test instance runs, close the bot window on your PC and remove its Startup shortcut.
Otherwise two copies of the test bot answer every click, which causes the "didn't respond in time" errors.

---

## 1. Create the server (5 min)

Any provider. The **smallest plan** is plenty for both instances (1 vCPU, 1–2 GB RAM):
- **Hetzner Cloud** (US: Ashburn, Hillsboro) or **DigitalOcean** (US regions)
- Image **Ubuntu 24.04 LTS**, a **US** region, an **SSH key** (or root password), name `aiavbot`

## 2. Set up the test instance first

```
ssh root@YOUR.SERVER.IP
curl -fsSL https://raw.githubusercontent.com/QuietOak/aiavbot/main/deploy/setup_vps.sh -o setup_vps.sh
sudo bash setup_vps.sh test
```
- **Token:** the *AIAVBot Test* token (hidden while you paste)
- **Server ID:** your **test server's** ID

You should see `aiavbot@test is running.` and `Logged in as … | instance: test · dev @ …`.
In the test server: `/aiav setup …` with the test channels, then `/aiav status`.

## 3. Set up the live instance

```
sudo bash setup_vps.sh live
```
- **Token:** the *AIAVBot* (live) token. The script refuses a token that another instance already uses.
- **Server ID:** the **Dreamers** server ID

In Dreamers (as an admin):
```
/aiav setup music:#multimeda-music-creation🎵🎥 collab:#aiav-club-collab-requests🏦 gallery:#aiav-club-gallery lounge:#aiav-club-lounge
/aiav modrole add role:@Moderator
/aiav status
```
Make sure the live bot's role can see and post in those channels (and any theme channels).
`/aiav status` should show no missing permissions, **Suno: ✅ working**, and `🧩 live · main @ …`.

## 4. Launch smoke test (live, 10 min)

- [ ] A Suno link in the music channel gets **🚀 Enhance Sharing** and **❤️ Like on Suno**
- [ ] Open the panel and add a line: the reply updates
- [ ] Collab requests: **Yes** → card + thread; **🙋 I'm interested** from another account
- [ ] A Stoop link or character card in collab requests → **🎭** character flow
- [ ] Gallery: **Yes** → congratulations card + comment thread
- [ ] `/aiav update` posts the activity update in the lounge
- [ ] Optional reboot test: `sudo reboot`, then after a minute both bots are back online

---

## Day-to-day commands (on the server)

| Task | test | live |
|---|---|---|
| Running? | `sudo systemctl status aiavbot@test` | `sudo systemctl status aiavbot@live` |
| Live log | `sudo journalctl -u aiavbot@test -f` | `sudo journalctl -u aiavbot@live -f` |
| Restart | `sudo systemctl restart aiavbot@test` | `sudo systemctl restart aiavbot@live` |
| **Update from GitHub** | `sudo aiavbot-update test` | `sudo aiavbot-update live` |
| Change token / server | `sudo nano /etc/aiavbot/test.env` + restart | `sudo nano /etc/aiavbot/live.env` + restart |
| Stop (e.g. test when idle) | `sudo systemctl stop aiavbot@test` | `sudo systemctl stop aiavbot@live` |

`sudo aiavbot-update all` updates both. Both instances list: `systemctl list-units 'aiavbot@*'`.

**Roll back** an instance (the update command prints the exact line):
```
sudo -u aiavbot git -C /opt/aiavbot/live checkout <commit>
sudo systemctl restart aiavbot@live
```
The next `sudo aiavbot-update live` returns to the latest `main`.

**Shared server files** (the service template, backup job and update command) are always taken from the
**live** instance's code, so an experiment on `dev` can't change how live runs. Changes to files in `deploy/`
take effect once merged to `main` and `sudo aiavbot-update live` runs.

## Backups and restore

Every instance is backed up daily, and before every update, into `/var/lib/aiavbot/<instance>/backups/` (14 days kept).

Restore (example: live):
```
sudo systemctl stop aiavbot@live
sudo cp /var/lib/aiavbot/live/backups/aiavbot-YYYY-MM-DD_HHMM.db /var/lib/aiavbot/live/aiavbot.db
sudo rm -f /var/lib/aiavbot/live/aiavbot.db-wal /var/lib/aiavbot/live/aiavbot.db-shm
sudo chown aiavbot:aiavbot /var/lib/aiavbot/live/aiavbot.db
sudo systemctl start aiavbot@live
```

Copy a backup to your PC (PowerShell): `scp root@YOUR.SERVER.IP:/var/lib/aiavbot/live/backups/aiavbot-….db .`

**Copy live data into test** (to test against real settings; use with care): stop test, copy
`/var/lib/aiavbot/live/aiavbot.db` over `/var/lib/aiavbot/test/aiavbot.db`, `chown aiavbot:aiavbot`, start test,
then run `/aiav setup` in the test server, because the channel settings point at Dreamers channels.

## Security notes

- Tokens live only in `/etc/aiavbot/<instance>.env` (root-only), never in the code folders or on GitHub.
- Both bots run as a no-login `aiavbot` user, and each instance can write only to its own `/var/lib/aiavbot/<instance>`.
- Firewall allows only SSH in. Security updates install automatically.
- **Token leaked?** Developer Portal → that bot → **Reset Token**, edit its env file, restart that instance.
- Optional once SSH keys work: set `PasswordAuthentication no` in `/etc/ssh/sshd_config`, then `sudo systemctl restart ssh`.
