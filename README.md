# ETHOL PENS Telegram Notifier

A small self-contained Python service that logs in to **ETHOL PENS** through the
campus CAS SSO and sends a Telegram message when:

- a **presensi** (attendance session) is opened for one of your courses,
- a **new tugas** (assignment) appears,
- an unsubmitted assignment is **less than 24 hours** or **less than 3 hours**
  from its deadline (optional).

The presensi message has a **✅ Presensi sekarang** button. The configured
Telegram admin can press it to record attendance without opening ETHOL
manually. Other members of a group/channel can see the message and the button,
but only the admin can use it.

It uses only the Python standard library. No `pip install` is needed.

---

## Portable package (moving to another machine)

Two archives are built next to the project:

| Archive | Contents | Use |
| --- | --- | --- |
| `ethol-notify-portable.zip` | Code, README, `.env.example`, service unit, `install.sh` | Safe to copy or share. Fill in the credentials after extraction. |
| `ethol-notify-full.zip` | Everything above plus `.env` and `state/` (cookies + SQLite) | Move the current working setup to another machine. **Contains secrets.** |

Quick start with the full archive:

```bash
unzip ethol-notify-full.zip -d ~/
cd ~/ethol-notify
chmod +x install.sh
./install.sh --systemd
```

`install.sh` copies the app to `~/ethol-notify` (or a directory you pass),
creates `state/` with mode `700`, keeps `.env` at mode `600`, and writes a
systemd unit whose paths match the target directory. Without `--systemd` it
only prepares the directory:

```bash
./install.sh /opt/ethol-notify
cd /opt/ethol-notify
python3 ethol_notify.py --once --dry-run
python3 ethol_notify.py --loop
```

If you move the **full** archive, stop the old instance first. The same bot
token must not be polled by two machines at the same time, otherwise Telegram
returns `409 Conflict` on `getUpdates`.

The full archive contains your bot token and ETHOL session cookies. Keep it
private (`chmod 600`). The session eventually expires; add `ETHOL_PASSWORD` or
`ETHOL_PASSWORD_FILE` to `.env` to allow fresh CAS logins on the new machine.

---

## 1. Create the Telegram bot

1. Open Telegram and message **[@BotFather](https://t.me/BotFather)**.
2. Send `/newbot`, choose a name and a username.
3. BotFather gives you a token like `123456789:AA...`. This is
   `TELEGRAM_BOT_TOKEN`.
4. Send any message to your new bot (or add it to a group and send a message
   there).
5. Find the chat id:

   ```bash
   curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" | python3 -m json.tool
   ```

   Look for `"chat": {"id": ...}`. This is `TELEGRAM_CHAT_ID`.
   For a group, the id is usually negative (for example `-1001234567890`).

---

## 2. Configure

```bash
cd ~/ethol-notify
cp .env.example .env
chmod 600 .env
nano .env
```

Fill in:

```dotenv
ETHOL_USERNAME=your_email@student.pens.ac.id
ETHOL_PASSWORD=your_password
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_CHAT_ID=123456789
TELEGRAM_ADMIN_USER_IDS=123456789
TELEGRAM_ALLOWED_CHAT_IDS=123456789
```

For a private chat, `TELEGRAM_CHAT_ID` and `TELEGRAM_ADMIN_USER_IDS` are the
same number. For a group or channel:

- set `TELEGRAM_CHAT_ID` to the group/channel id (usually starts with `-100`),
- add that id to `TELEGRAM_ALLOWED_CHAT_IDS`,
- set `TELEGRAM_ADMIN_USER_IDS` to **your personal Telegram user id**, not the
  group id.

You can get your personal user id by sending `/id` to the bot after it is
running, or by asking [@userinfobot](https://t.me/userinfobot).

If you do not want the password inside `.env`, use a separate file:

```bash
mkdir -p ~/.config/ethol-notify
chmod 700 ~/.config/ethol-notify
printf '%s' 'your_password' > ~/.config/ethol-notify/password
chmod 600 ~/.config/ethol-notify/password
```

Then set this in `.env` and leave `ETHOL_PASSWORD` empty:

```dotenv
ETHOL_PASSWORD_FILE=/home/YOUR_USER/.config/ethol-notify/password
```

The script never prints or logs the password. Session cookies are stored in
`state/cookies.txt` with mode `600`.

---

## 3. Test

```bash
cd ~/ethol-notify

# Parse and print the messages without sending anything to Telegram.
# State is kept in memory, so this does not affect the real run.
python3 ethol_notify.py --once --dry-run

# Check Telegram delivery.
python3 ethol_notify.py --test-telegram

# One real poll.
python3 ethol_notify.py --once
```

The first real poll sends a startup message and a one-time summary of
currently unsubmitted tasks. Later polls only send new events.

---

## 4. Run continuously

### Foreground

```bash
python3 ethol_notify.py --loop
```

### systemd (recommended)

```bash
./install.sh --systemd
```

Or install the unit manually (edit `WorkingDirectory`, `ExecStart`, and the
paths first if the project is not in `/home/ubuntu/ethol-notify`):

```bash
sudo cp ethol-notify.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ethol-notify
sudo systemctl status ethol-notify
sudo journalctl -u ethol-notify -f
```

The service restarts automatically if the process exits. It reads `.env` and
writes only to the `state` directory next to the script.

If you edit `.env`, restart it:

```bash
sudo systemctl restart ethol-notify
```

> The interactive Telegram buttons only work while the service is running in
> `--loop` mode. `--once` sends the messages and exits, so there is no listener
> to receive the button press.

---

## Presensi directly from Telegram

When a presensi opens, the bot sends a message like:

```
🔔 Presensi Dibuka

Pemrograman Game Berbasis Jaringan
Kelas: 9 STr Game A
Waktu: ...

Segera isi presensi sebelum sesi ditutup.
Admin bisa menekan tombol ✅ Presensi sekarang di bawah...

[✅ Presensi sekarang]  [🌐 Buka ETHOL]
```

Only the user ids in `TELEGRAM_ADMIN_USER_IDS` can press the first button.
Anyone else who presses it gets a private alert saying that only the admin can
record attendance. The admin's button calls the same ETHOL endpoint as the
website (`/api/presensi/mahasiswa`), so the attendance is recorded for the same
account used by the notifier.

Telegram commands:

| Command | Purpose |
| --- | --- |
| `/status` | Last poll time, course count, active presensi, unsubmitted tasks |
| `/presensi` | Show the currently open presensi sessions with buttons |
| `/id` | Show the current chat id and your Telegram user id |
| `/help` | Help text |

The bot only responds in chats listed in `TELEGRAM_ALLOWED_CHAT_IDS`.

---

## How it works

1. **Login** — follows `/api/auth/cas-redirect`, parses the PENS CAS login
   form, submits the username/password, and stores the resulting cookies.
2. **Session refresh** — the access token is short-lived. On HTTP 401 the
   script calls `/api/auth/refresh`. If that fails it performs a fresh CAS
   login (using `ETHOL_PASSWORD` or `ETHOL_PASSWORD_FILE`).
3. **Courses** — reads `/api/auth/config` for the active year/semester, then
   `/api/kuliah?tahun=...&semester=...`.
4. **Presensi** — calls `/api/presensi/aktif-kuliah` for every course. An
   entry with `open = 1` means attendance is open. It also checks
   `/api/presensi/riwayat` so it does not notify you for a session you already
   attended.
5. **Tugas** — calls `/api/tugas?kuliah=...&jenisSchema=...` for every course.
   New task ids are remembered in SQLite; submitted tasks are ignored.
6. **Telegram** — sends an HTML message through the Bot API. Messages include
   a direct link to the course/task page in ETHOL.
7. **Telegram listener** — in `--loop` mode a background thread long-polls
   `getUpdates` for commands and button presses. The presensi button validates
   the sender against `TELEGRAM_ADMIN_USER_IDS`, posts to
   `/api/presensi/mahasiswa`, then edits the original message to show the
   result and removes the button.

State is stored in `state/state.db` (SQLite) and `state/cookies.txt`.

---

## Commands

| Command | Purpose |
| --- | --- |
| `python3 ethol_notify.py --once` | One poll |
| `python3 ethol_notify.py --loop` | Poll forever |
| `python3 ethol_notify.py --loop --interval 180` | Poll every 3 minutes |
| `python3 ethol_notify.py --dry-run` | Print messages, do not send |
| `python3 ethol_notify.py --test-telegram` | Send a test Telegram message |
| `python3 ethol_notify.py --login` | Force a fresh CAS login |
| `python3 ethol_notify.py --no-initial-summary` | Skip the first-run task list |
| `python3 ethol_notify.py --verbose` | Debug logging |

Useful environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CHECK_INTERVAL_SECONDS` | `180` | Seconds between polls |
| `HTTP_TIMEOUT_SECONDS` | `20` | Per-request timeout |
| `REMIND_TASK_24H` | `1` | Send a reminder below 24 hours |
| `REMIND_TASK_3H` | `1` | Send a reminder below 3 hours |
| `AUTO_PRESENSI` | `0` | Auto-record presensi after a delay (toggleable from Telegram) |
| `AUTO_PRESENSI_DELAY_SECONDS` | `600` | Minimum wait before auto presensi, in seconds (10 minutes); it never fires immediately |
| `TELEGRAM_ADMIN_USER_IDS` | chat id | User ids allowed to press presensi |
| `TELEGRAM_ALLOWED_CHAT_IDS` | chat id | Chats the bot may respond to |
| `STATE_DIR` | `./state` | Cookie + SQLite location |

---

## Notes and limitations

- The bot can record attendance when the admin presses the button, or
  **automatically** when `AUTO_PRESENSI=1` (or the `enable auto ethol ✅`
  button) is active. Auto presensi never fires immediately: the first poll only
  schedules it, and the submission happens at the first poll at least
  `AUTO_PRESENSI_DELAY_SECONDS` (default 600 = 10 minutes) after detection.
  Pressing **✅ Presensi sekarang** cancels the scheduled auto submission, and
  **disable auto ethol ❌** cancels every pending one. It never uploads
  assignments. Only admin user ids can toggle it. The choice is stored in
  `state/state.db`, so it survives restarts.
- If the bot is in a public group/channel, everyone can see the notifications.
  Only the configured admin user ids can use the presensi button.
- ETHOL must be reachable from the machine running the script.
- The SSO login form can change. If CAS starts failing, run
  `--login --verbose` and check the error.
- The script polls each course separately (about 20 requests per poll). Keep
  the interval at 3 minutes or more to be polite to the campus server.
- The first poll may take 20–30 seconds because it fetches every course.
- If the refresh token expires and no password is configured, the service can
  no longer log in. It will send an error alert and wait for a new
  `--login` (or configure `ETHOL_PASSWORD_FILE`).
- Do not commit `.env`, `state/cookies.txt`, or `state/state.db` to git.

---

## Troubleshooting

**`Username atau password salah` / CAS rejects the login**
Check the password. Student accounts use SSO, not the "Dosen Luar Biasa" local
login form.

**`Telegram HTTP 401`**
The bot token is wrong.

**`Telegram HTTP 400: chat not found`**
Send a message to the bot first, then re-check the chat id. For groups, make
sure the bot is a member and has permission to send messages.

**`Telegram HTTP 403: bot was blocked by the user`**
Unblock the bot or send `/start` again.

**`Session expired` repeated**
Set `ETHOL_PASSWORD` or `ETHOL_PASSWORD_FILE`, or run `--login` manually.

**No notifications although a presensi is open**
Check `journalctl -u ethol-notify -f`. The script skips sessions you have
already attended and skips a session after the first notification.

**The presensi button does nothing or says "only admin"**
For a private chat, `TELEGRAM_ADMIN_USER_IDS` should equal your chat id. For a
group/channel, it must be your **personal** Telegram user id, not the group id.
Send `/id` to the bot to check.

**The presensi button says "Presensi sudah ditutup"**
The lecturer closed the session before the button was pressed. The bot cannot
override that; use the ETHOL page if the lecturer reopens it.

**The bot does not respond to /status**
Make sure the service is running in `--loop` mode, the chat id is in
`TELEGRAM_ALLOWED_CHAT_IDS`, and only one instance of the service is running
(Two instances cause a Telegram `409 Conflict` on `getUpdates`).
