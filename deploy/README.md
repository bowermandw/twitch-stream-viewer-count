# Running on a server

## Requirements

**Python 3.9 or newer. Nothing else.**

There are no third-party Python packages — no `pip install` step, no virtualenv
needed, nothing to keep patched. The project uses only the standard library:
`urllib` for HTTP, `csv` for storage, and hand-built SVG for the charts.

```
python3 --version      # 3.9+
```

That's the whole Python dependency list. `requirements.txt` is intentionally
empty and says so.

**Two system packages, only if you want the [daily Drive report](#daily-report-to-google-drive):**

```
apt install librsvg2-bin      # rsvg-convert — the daily job exits 1 without it
apt install fonts-dejavu-core # or the PNGs come out with no text in them
```

Neither is a Python package, so there is still nothing for pip to do. Polling
and `graph` need neither.

## Install

```
sudo mkdir -p /opt/twitch-metrics
sudo chown $USER /opt/twitch-metrics
git clone https://github.com/bowermandw/twitch-stream-viewer-count.git /opt/twitch-metrics
cd /opt/twitch-metrics
```

Then either run it in place:

```
python3 -m twitchmetrics --help
```

or install a `twitch-metrics` command on the PATH.

**Watch out:** distro and Homebrew Pythons are *externally managed* (PEP 668)
and will refuse a bare `pip install` with an `externally-managed-environment`
error. Use a venv:

```
python3 -m venv /opt/twitch-metrics/.venv
/opt/twitch-metrics/.venv/bin/pip install -e /opt/twitch-metrics
/opt/twitch-metrics/.venv/bin/twitch-metrics --help
```

or `pipx install .` if you have pipx. Either way nothing is downloaded — the
install only creates the entry point, because there are no dependencies.

Honestly, on a server the simplest thing is to skip installing and use
`python3 -m twitchmetrics`, which is what the systemd unit does. A venv here
buys you a tidier command name, not dependency isolation — there are no
dependencies to isolate.

## Credentials

```
python3 -m twitchmetrics setup --client-id XXXX --client-secret YYYY
```

Use the non-interactive form on a headless box — the interactive prompt offers
to open a browser, which a server has no use for. This writes `.env` at mode
0600 after verifying the credentials against the live API.

## The one awkward part: the user token

Viewers and followers need only the app token, which works headlessly. **Chat
size needs a user token, and getting one requires a browser.**

Authorizing straight from a server doesn't work on its own: you paste the URL
into a browser on your desktop, approve, and the redirect to
`http://localhost:3000` resolves to **your desktop's** localhost, not the
server's. The server sits waiting for a callback that never arrives.

### A. SSH tunnel — the normal flow, made to work

Forward your desktop's port 3000 to the server's, so the redirect reaches the
listener.

**On your desktop**, open the tunnel and leave it running:

```
ssh -L 3000:localhost:3000 user@your-server
```

**In that same session** (or any other shell on the server):

```
cd /opt/twitch-metrics
python3 -m twitchmetrics auth --no-browser
```

Copy the printed URL into your desktop browser and approve. The redirect now
travels back down the tunnel, the server catches it, and you get:

```
Authorized as yourname (user id 12345678).
```

Close the tunnel afterwards — it's only needed for this one step. The refresh
token keeps the credential alive from then on.

### B. Paste the code back — no tunnel needed

```
python3 -m twitchmetrics auth --manual
```

It prints the authorize URL. Open that in a browser on any machine and approve.
The browser then tries to reach `http://localhost:3000` and **fails to connect**
— expected. The address bar still holds the code:

```
http://localhost:3000/?code=k2p9x...&scope=moderator%3Aread%3Achatters&state=...
```

Copy that whole URL and paste it at the prompt. The `state` parameter is checked
against the one just issued, so the CSRF protection survives the detour.

Useful when you can't open a tunnel — a jump host, a locked-down bastion, or a
console session with no SSH of your own.

### C. Authorize on your laptop, copy the token up

If you already have the project checked out locally with the same credentials:

```
# on your laptop
python3 -m twitchmetrics auth
scp data/.user_token.json server:/opt/twitch-metrics/data/
ssh server chmod 600 /opt/twitch-metrics/data/.user_token.json
```

### D. Skip it

`--no-chatters` records viewers and followers only, and needs no user token at
all:

```
python3 -m twitchmetrics poll themeparkgiant --no-chatters
```

## Run it in the background

### systemd — the right answer for a Linux server

The unit is a **template**, so one file serves any number of channels. The name
after the `@` becomes the channel.

```
sudo cp deploy/twitch-metrics@.service /etc/systemd/system/
sudoedit /etc/systemd/system/twitch-metrics@.service   # set User, Group, WorkingDirectory
sudo systemctl daemon-reload

sudo systemctl enable --now twitch-metrics@themeparkgiant
sudo systemctl enable --now twitch-metrics@prgskidmark
```

Both run simultaneously and independently:

```
systemctl status twitch-metrics@themeparkgiant
journalctl -u twitch-metrics@prgskidmark -f
systemctl restart twitch-metrics@themeparkgiant
systemctl stop 'twitch-metrics@*'          # all of them
systemctl list-units 'twitch-metrics@*'    # what's running
```

Each instance writes its own `data/metrics_<channel>.csv` and
`data/metrics_<channel>.log`. One crashing, restarting or being stopped has no
effect on the others.

The only state they share is the cached tokens in `data/`. Those are written
atomically (temp file plus rename, so a reader never sees a half-written file),
and the user-token refresh is serialised with a file lock — Twitch rotates the
refresh token on use, so two pollers refreshing at the same instant would
otherwise leave one holding an invalidated credential. Whichever poller waits
picks up the other's fresh token instead of rotating again.

`systemctl stop` sends SIGTERM, which the poller handles: it finishes the
current sample, writes a summary line and exits 0 — it does not die mid-write,
and it does not sit out the rest of the polling interval first.

### The YouTube poller

`youtube-metrics@.service` is a second template, set up the same way and running
alongside the Twitch one rather than instead of it:

```
sudo cp deploy/youtube-metrics@.service /etc/systemd/system/
sudoedit /etc/systemd/system/youtube-metrics@.service   # set User, Group, WorkingDirectory
sudo systemctl daemon-reload

sudo systemctl enable --now youtube-metrics@themeparkgiant
```

The name after the `@` is the YouTube handle — the `@name` in the channel URL,
without the `@` — which need not match the Twitch login. Instances write
`data/youtube_<channel>.csv` and `data/youtube_<channel>.log`, so a creator can
be polled on both platforms at once with no shared files at all: this poller
uses only `YOUTUBE_API_KEY`, and touches none of the cached Twitch tokens.

The one thing to watch is quota rather than rate limiting. Each sample costs
three of the 10,000 YouTube API units allowed per day, and the default 60-second
interval spends 4,320 of them — fine for one channel, fine for two, over budget
for three. Stretch it with `YOUTUBE_INTERVAL` in `.env`, or `--interval` in the
unit, if you enable more than two instances:

```
sudo systemctl edit youtube-metrics@thirdchannel
# [Service]
# Environment=YOUTUBE_INTERVAL=180
```

The poller logs its projected daily usage at startup and refuses to run faster
than every 60 seconds.

### If it won't start

`journalctl -u twitch-metrics@themeparkgiant -n 50` almost always says why. The
common ones, all caused by the template's placeholders not matching your
install:

| Journal says | Cause | Fix |
|---|---|---|
| `status=217/USER` | `User=twitch` doesn't exist on this machine | create it, or set `User=` to an account that does |
| `status=200/CHDIR` | `WorkingDirectory` doesn't exist | point it at your actual checkout |
| `No module named twitchmetrics` | right directory, wrong interpreter or cwd | check `ExecStart` python path and `WorkingDirectory` |
| starts, then `Permission denied` writing the CSV | `ProtectSystem`/`ProtectHome` blocking the data dir | add the path to `ReadWritePaths` |

`217/USER` is the one that catches people, because systemd fails *before*
running anything: there is no Python traceback, the service just restart-loops,
and nothing is ever written. `systemctl status` shows `activating
(auto-restart)` rather than `failed`, which reads like it's still coming up.

**Check what systemd actually resolved** rather than what you think the file
says:

```
systemctl show -p User -p WorkingDirectory -p ExecStart twitch-metrics@themeparkgiant
```

### Keeping the checkout in a home directory

If it lives in `/root` or `/home/you` rather than `/opt`, the template's
`ProtectHome=read-only` makes that path unwritable and the poller can't write
its CSV. Set `ProtectHome=no` and point `ReadWritePaths` at the data directory.

### Drop-ins on a template unit

`systemctl edit twitch-metrics@themeparkgiant` writes to
`twitch-metrics@themeparkgiant.service.d/`, which applies to that instance only.
Settings meant for every channel belong in the template file itself — editing
`/etc/systemd/system/twitch-metrics@.service` is the reliable way to change
`User`, `WorkingDirectory` and friends for all of them.

### Changing the polling interval

`TWITCH_INTERVAL` in `.env` applies to every instance:

```
TWITCH_INTERVAL=60
```

For a single instance, add an override rather than editing the shared template:

```
sudo systemctl edit twitch-metrics@themeparkgiant
```

```ini
[Service]
Environment=TWITCH_INTERVAL=60
```

Then `sudo systemctl restart twitch-metrics@themeparkgiant`. Overrides live in
`/etc/systemd/system/twitch-metrics@themeparkgiant.service.d/` and survive
updates to the template.

### tmux — quickest thing that survives disconnecting

No root, no unit file. Good for trying it out before committing to a service.

```
tmux new -s twitch
python3 -m twitchmetrics poll themeparkgiant
# Ctrl-B then D to detach; the poller keeps running
```

```
tmux attach -t twitch     # come back to it
tmux ls                   # what's running
```

Does **not** survive a reboot. `screen -S twitch` works the same way.

### nohup — one command, no dependencies

```
nohup python3 -m twitchmetrics poll themeparkgiant > /dev/null 2>&1 &
echo $! > /tmp/twitch.pid
```

Output still goes to `data/metrics_<channel>.log`, which is why stdout can be
discarded. To stop it:

```
kill $(cat /tmp/twitch.pid)
```

That sends SIGTERM, so it shuts down cleanly. Also does not survive a reboot.

### cron — if you would rather not have a long-running process

`--once` takes a single sample and exits, so cron can drive the schedule
instead:

```
*/5 * * * * cd /opt/twitch-metrics && /usr/bin/python3 -m twitchmetrics poll themeparkgiant --once >> data/cron.log 2>&1
```

The trade-off: a fresh process every five minutes re-reads the token cache and
re-resolves the channel id, and cron's sparse environment is a common source of
"works in my shell, not in cron" problems. Prefer systemd unless you have a
reason.

### macOS

`launchd` is the local equivalent of systemd. A minimal agent at
`~/Library/LaunchAgents/com.you.twitchmetrics.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.you.twitchmetrics</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/python3</string>
    <string>-m</string><string>twitchmetrics</string>
    <string>poll</string><string>themeparkgiant</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/you/Dev/twitch-stream-viewer-count</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```

```
launchctl load ~/Library/LaunchAgents/com.you.twitchmetrics.plist
launchctl unload ~/Library/LaunchAgents/com.you.twitchmetrics.plist
```

Use an absolute path to the interpreter — launchd does not inherit your shell
PATH.

## Log growth

`data/*.log` grows without bound. At a 5-minute interval that is roughly 2 MB a
year, so it is not urgent, but for a permanent install:

```
sudo tee /etc/logrotate.d/twitch-metrics <<'CONF'
/opt/twitch-metrics/data/*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
CONF
```

`copytruncate` matters — the poller holds no persistent handle, but it appends,
and rotating out from under it without truncating would leave a sparse file.

The CSVs are the data and should not be rotated.

## PNG conversion

Charts are SVG, which any browser renders. `daily` converts them to PNG because
Drive previews PNG properly and mostly offers an SVG as a download.

```
apt install librsvg2-bin           # Debian/Ubuntu
brew install librsvg               # macOS
rsvg-convert -w 1600 charts/chart_themeparkgiant_metrics.svg -o chart.png
```

Required by `daily`, which checks for it at startup and exits 1 with that
install line if it is missing. Nothing else here needs it.

**Fonts.** A minimal server image often has none, and `rsvg-convert` will
cheerfully exit 0 having written a PNG with the text invisible or boxed. `apt
install fonts-dejavu-core` covers it — the SVG asks for Roboto, then system-ui,
Helvetica, Arial, sans-serif. Nothing can detect this automatically, so look at
the first uploaded PNG once.

You may also see `Fontconfig error: No writable cache directories` on stderr
under the unit's `ProtectHome=read-only`. That is not a failure — the conversion
is judged by exit status and the PNG's magic bytes, not by stderr.

## Daily report to Google Drive

One `oneshot` service plus a timer. Charts every channel you are polling,
converts each to PNG, uploads to `Twitch Metrics/<channel>/<date>.png` in your
own Drive.

### 1. Authorize, once, interactively

The service runs headless and will never prompt — it exits with an actionable
message instead. So do the browser step first, as the **same user the service
will run as**, since the token is 0600 in the shared `data/`:

```
python3 -m twitchmetrics drive --setup
```

Read what it prints about **publishing the consent screen**: left in "Testing",
Google expires refresh tokens after 7 days and the daily upload dies mid-week.

On a headless box the redirect needs the same treatment as `auth` — an SSH
tunnel from the machine with the browser:

```
ssh -L 3000:localhost:3000 user@your-server        # on your desktop, leave open
sudo -u twitch python3 -m twitchmetrics drive --auth --no-browser
```

or no tunnel at all:

```
sudo -u twitch python3 -m twitchmetrics drive --auth --manual
```

Both flows use port 3000, the same as `twitch-metrics auth`, so they can't run
at the same moment. `GOOGLE_REDIRECT_URI` moves one if you need to.

Confirm it took, without uploading anything:

```
python3 -m twitchmetrics drive --status
python3 -m twitchmetrics drive --check     # non-interactive, exactly as the service runs
```

### 2. Install the units

```
sudo cp deploy/twitch-metrics-daily.service /etc/systemd/system/
sudo cp deploy/twitch-metrics-daily.timer   /etc/systemd/system/
sudoedit /etc/systemd/system/twitch-metrics-daily.service   # User, Group, WorkingDirectory
sudo systemctl daemon-reload
```

**Enable the timer, not the service.** The service has no `[Install]` section on
purpose: enabling it would run the report once at every boot as well.

### 3. Test it before trusting the schedule

```
python3 -m twitchmetrics daily --list-channels   # does discovery see your pollers?
python3 -m twitchmetrics daily --dry-run         # render and convert, no upload
systemd-analyze calendar '17:00'                 # what that means in this timezone

sudo systemctl start twitch-metrics-daily.service
journalctl -u twitch-metrics-daily.service -n 60 --no-pager

sudo systemctl enable --now twitch-metrics-daily.timer
systemctl list-timers 'twitch-metrics*'
```

`list-timers` shows the *randomized* next run, so 17:01:43 for a `17:00` unit is
correct — the unit adds up to two minutes of jitter.

### Channels, and what red means

The channel list comes from the enabled `twitch-metrics@` instances, so a poller
you enable is automatically in the report. If the pollers aren't systemd-managed
here, name them instead — in `.env`:

```
TWITCH_DAILY_CHANNELS=themeparkgiant,prgskidmark
```

or as a drop-in, `sudo systemctl edit twitch-metrics-daily.service`:

```ini
[Service]
Environment=TWITCH_DAILY_CHANNELS=themeparkgiant,prgskidmark
```

Exit 0 means every channel was uploaded, or skipped because that channel simply
didn't stream. Exit 1 means something is actually wrong — a poller that stopped,
a failed upload, no channels found at all. That distinction is the point: an
alarm that goes red every time you take a day off is an alarm you stop reading.

### Missed runs

The timer is deliberately **not** `Persistent=true`. A catch-up run isn't told
which day it missed, so `--date today` after a post-midnight boot would chart the
nearly-empty new day and upload it under the wrong name. Backfill by hand:

```
python3 -m twitchmetrics daily --date yesterday
```

### If the daily job misbehaves

| Journal says | Cause | Fix |
|---|---|---|
| `status=217/USER` | `User=twitch` doesn't exist | create it, or use an existing account |
| `rsvg-convert is not installed` | librsvg missing | `apt install librsvg2-bin` |
| uploads fine, but the PNG has no text | no fonts on the box | `apt install fonts-dejavu-core` |
| `No channels to report on` | no enabled `twitch-metrics@` instances | `systemctl enable twitch-metrics@yourchannel`, or set `TWITCH_DAILY_CHANNELS` |
| `no samples at all on <date>` | that channel's poller isn't running | `systemctl status 'twitch-metrics@*'` |
| `offline all day, nothing to chart` | the channel didn't stream — not an error | nothing |
| `No Google Drive authorization` | never authorized, or as the wrong user | `sudo -u twitch python3 -m twitchmetrics drive --auth --manual` |
| `invalid_grant` | consent screen still in "Testing" (7-day expiry) | publish it, then `drive --auth --force` |
| the timer never fires | the *service* was enabled instead of the timer | `systemctl disable twitch-metrics-daily.service && systemctl enable --now twitch-metrics-daily.timer` |
| `Permission denied` writing a PNG | checkout under `/home` or `/root` | `ProtectHome=no` |

### Chart growth

`daily` keeps both the SVG and the PNG in `charts/`, which is useful when an
upload fails and you want to see what it was going to send — but it grows
without bound, roughly 0.5 GB a year at four channels. They all regenerate from
the CSVs, so pruning is safe:

```
find /opt/twitch-metrics/charts -name '*.png' -mtime +90 -delete
```

Never rotate `data/*.csv`. That is the actual data.

## Where things live

| Path | Contents |
|---|---|
| `data/` | sample CSVs (Twitch and YouTube), poll logs, `daily.log`, cached tokens |
| `charts/` | generated SVGs, and the PNGs `daily` uploads |
| `.env` | credentials, mode 0600 |

`data/daily.log` is already covered by the `data/*.log` logrotate glob above.

Point them elsewhere with `TWITCH_DATA_DIR` and `TWITCH_CHARTS_DIR` if you'd
rather keep data on a mounted volume:

```
TWITCH_DATA_DIR=/var/lib/twitch-metrics python3 -m twitchmetrics poll
```

## Backups

`data/*.csv` is the only irreplaceable thing here — charts regenerate from it.
The token files are recoverable by re-authorizing, and should not be backed up
to anywhere less private than the server itself.
