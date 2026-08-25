# Running on a server

## Requirements

**Python 3.9 or newer. Nothing else.**

There are no third-party Python packages — no `pip install` step, no virtualenv
needed, nothing to keep patched. The project uses only the standard library:
`urllib` for HTTP, `csv` for storage, and hand-built SVG for the charts.

```
python3 --version      # 3.9+
```

That is the whole dependency list for a box that only collects, because a poller
that cannot reach the database writes to `data/*.csv` instead and replays the gap
later. `requirements.txt` is intentionally empty and explains itself.

**Two pip packages, each only for what it names:**

```
pip install 'psycopg[binary]'   # reading and writing the sample database
pip install boto3               # publishing the daily report to S3
```

Both are imported lazily, so a box that only polls needs neither and the CLI
works in full without them. `[binary]` ships a prebuilt libpq wheel — no
`apt install`, no compiler. No system packages at all: the report publishes SVG, which
browsers render natively, so `rsvg-convert` and a font package — both PNG
requirements — are no longer involved.

## The database

The pollers write samples to PostgreSQL and fall back to `data/*.csv` when they
cannot reach it, replaying the gap themselves once it answers. Two things about
that are worth knowing before you debug the wrong problem.

**A CSV that has stopped growing is the healthy state.** It is a spool now. Every
habit built on `tail -f data/metrics_x.csv` and `wc -l data/*.csv` has inverted:
a file sitting at the same size means every sample is reaching the database. Ask
the database instead:

```
twitch-metrics db --status
```

**Use `host:port`, never a Unix socket.** All three units here set
`ProtectSystem=strict` with `ReadWritePaths=` covering only `data/` and
`charts/`. Debian's socket lives in `/run/postgresql`, and connecting to a socket
needs *write* access to that directory — so `postgresql:///twitchmetrics` fails
with an error that reads like a `pg_hba.conf` problem and is not one. Either:

```
TWITCH_DATABASE_URL=postgresql://twitch:PASSWORD@127.0.0.1:5432/twitchmetrics
```

or add `ReadWritePaths=/run/postgresql` to each unit. The first is better: it is
also what keeps working the day the database moves to another host.

**Back it up.** `data/` used to hold the only irreplaceable thing on the machine;
the database does now:

```
0 3 * * *  pg_dump --format=custom twitchmetrics > /var/backups/tm-$(date +\%F).dump
```

Note the escaped `%` — cron treats a bare one as a newline.


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
python3 -m twitchmetrics poll testchannel --no-chatters
```

## Run it in the background

### systemd — the right answer for a Linux server

The unit is a **template**, so one file serves any number of channels. The name
after the `@` becomes the channel.

```
sudo cp deploy/twitch-metrics@.service /etc/systemd/system/
sudoedit /etc/systemd/system/twitch-metrics@.service   # set User, Group, WorkingDirectory
sudo systemctl daemon-reload

sudo systemctl enable --now twitch-metrics@testchannel
sudo systemctl enable --now twitch-metrics@prgskidmark
```

Both run simultaneously and independently:

```
systemctl status twitch-metrics@testchannel
journalctl -u twitch-metrics@prgskidmark -f
systemctl restart twitch-metrics@testchannel
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

sudo systemctl enable --now youtube-metrics@testchannel
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

`journalctl -u twitch-metrics@testchannel -n 50` almost always says why. The
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
systemctl show -p User -p WorkingDirectory -p ExecStart twitch-metrics@testchannel
```

### Keeping the checkout in a home directory

If it lives in `/root` or `/home/you` rather than `/opt`, the template's
`ProtectHome=read-only` makes that path unwritable and the poller can't write
its CSV. Set `ProtectHome=no` and point `ReadWritePaths` at the data directory.

### Drop-ins on a template unit

`systemctl edit twitch-metrics@testchannel` writes to
`twitch-metrics@testchannel.service.d/`, which applies to that instance only.
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
sudo systemctl edit twitch-metrics@testchannel
```

```ini
[Service]
Environment=TWITCH_INTERVAL=60
```

Then `sudo systemctl restart twitch-metrics@testchannel`. Overrides live in
`/etc/systemd/system/twitch-metrics@testchannel.service.d/` and survive
updates to the template.

### tmux — quickest thing that survives disconnecting

No root, no unit file. Good for trying it out before committing to a service.

```
tmux new -s twitch
python3 -m twitchmetrics poll testchannel
# Ctrl-B then D to detach; the poller keeps running
```

```
tmux attach -t twitch     # come back to it
tmux ls                   # what's running
```

Does **not** survive a reboot. `screen -S twitch` works the same way.

### nohup — one command, no dependencies

```
nohup python3 -m twitchmetrics poll testchannel > /dev/null 2>&1 &
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
*/5 * * * * cd /opt/twitch-metrics && /usr/bin/python3 -m twitchmetrics poll testchannel --once >> data/cron.log 2>&1
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
    <string>poll</string><string>testchannel</string>
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

## Converting a chart to PNG by hand

Nothing needs this any more — the website serves SVG, which any browser renders
— but a PNG is still easier to paste into Discord or a message:

```
apt install librsvg2-bin           # Debian/Ubuntu
brew install librsvg               # macOS
rsvg-convert -w 1600 charts/chart_testchannel_metrics.svg -o chart.png
```

A minimal server image often has no fonts, and `rsvg-convert` will cheerfully
exit 0 having written a PNG with the text invisible. `apt install
fonts-dejavu-core` covers it. You may also see `Fontconfig error: No writable
cache directories` on stderr under a unit's `ProtectHome=read-only`; that is not
a failure.

## Daily report

One run charts every polled channel, both platforms, and publishes each to its
own S3 static website. See the [README](../README.md#the-website) for what the
page looks like.

### 1. AWS, once

```
pip install boto3
```

Create an IAM user with programmatic access and the policy in the
[README](../README.md#setting-up-aws) — scoped to `arn:aws:s3:::tm-*`, so a
mistake here cannot reach anything else in the account. Then either put the key
in `.env`:

```
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1
```

or leave those unset and let boto3 use `~/.aws/credentials` or, on EC2, an
instance role — which is better, since there is then no long-lived key on the
box at all.

**Turn off account-level Block Public Access**: *S3 → Block Public Access
(account settings) → Edit → clear all four*. It is a separate setting from the
per-bucket one, AWS applies whichever is more restrictive, and leaving it on
makes every bucket policy fail with `AccessDenied`.

Unlike the Drive report this replaced, there is no browser login and no token to
keep alive, so nothing here needs an SSH tunnel and nothing expires after 7 days.

### 2. One bucket per channel

```
sudo -u twitch python3 -m twitchmetrics s3 --setup testchannel
```

Prints the website URL. Re-runnable: a channel that already has a bucket is left
alone. The name is random, because bucket names are global, and is recorded in
`data/.s3_buckets.json` — **back that file up**, or you lose track of which
bucket belongs to which channel.

Confirm it took, publishing nothing:

```
sudo -u twitch python3 -m twitchmetrics s3 --check testchannel
sudo -u twitch python3 -m twitchmetrics s3 --list
```

### 3. Install the units

```
sudo cp deploy/twitch-metrics-daily.service /etc/systemd/system/
sudo cp deploy/twitch-metrics-daily.timer   /etc/systemd/system/
sudoedit /etc/systemd/system/twitch-metrics-daily.service   # User, Group, WorkingDirectory
sudo systemctl daemon-reload
```

**Enable the timer, not the service.** The service has no `[Install]` section on
purpose: enabling it would run the report once at every boot as well.

### 4. Test it before trusting the schedule

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

The channel list comes from the enabled `twitch-metrics@` and
`youtube-metrics@` instances, so a poller you enable is automatically in the
report, and a channel with both is listed once. If the pollers aren't systemd-managed
here, name them instead — in `.env`:

```
TWITCH_DAILY_CHANNELS=testchannel,prgskidmark
```

or as a drop-in, `sudo systemctl edit twitch-metrics-daily.service`:

```ini
[Service]
Environment=TWITCH_DAILY_CHANNELS=testchannel,prgskidmark
```

Exit 0 means every channel was published, or skipped because that channel
simply didn't stream — including a channel you only poll on one platform. Exit 1
means something is actually wrong: a poller that stopped, a failed upload, no
channels found at all. That distinction is the point: an
alarm that goes red every time you take a day off is an alarm you stop reading.

### Missed runs

The timer is deliberately **not** `Persistent=true`. A catch-up run isn't told
which day it missed, so `--date today` after a post-midnight boot would chart the
nearly-empty new day and publish it under the wrong date. Backfill by hand:

```
python3 -m twitchmetrics daily --date yesterday
```

### If the daily job misbehaves

| Journal says | Cause | Fix |
|---|---|---|
| `status=217/USER` | `User=twitch` doesn't exist | create it, or use an existing account |
| `needs boto3, which isn't installed` | publishing needs it; collecting doesn't | `pip install boto3` |
| `No channels to report on` | no enabled poller instances | `systemctl enable twitch-metrics@yourchannel`, or set `TWITCH_DAILY_CHANNELS` |
| `no samples at all today` | that platform's poller isn't running | `systemctl status 'twitch-metrics@*' 'youtube-metrics@*'` |
| `offline all day, nothing to chart` | the channel didn't stream — not an error | nothing |
| `nothing to backfill` | that CSV doesn't reach back that far — not an error | nothing |
| `No S3 bucket for '<channel>' yet` | `--setup` never run for it | `s3 --setup <channel>` |
| `AWS rejected these credentials` | wrong or missing key | check `.env`, or `s3 --check` |
| `refused the public-read policy` | account-level Block Public Access is on | clear it in the S3 console |
| the timer never fires | the *service* was enabled instead of the timer | `systemctl disable twitch-metrics-daily.service && systemctl enable --now twitch-metrics-daily.timer` |
| `Permission denied` writing a chart | checkout under `/home` or `/root` | `ProtectHome=no` |

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
| PostgreSQL | the samples, the broadcasts and the report tables — the irreplaceable part |
| `data/` | the CSV spool (written only during an outage), poll logs, `daily.log`, cached tokens, `.s3_buckets.json` |
| `charts/` | generated SVGs, which is what `daily` publishes |
| `.env` | credentials, mode 0600 |

`data/daily.log` is already covered by the `data/*.log` logrotate glob above.

Point them elsewhere with `TWITCH_DATA_DIR` and `TWITCH_CHARTS_DIR` if you'd
rather keep data on a mounted volume:

```
TWITCH_DATA_DIR=/var/lib/twitch-metrics python3 -m twitchmetrics poll
```

## Backups

**The database is the irreplaceable thing now.** Charts regenerate from it and so
does every page in S3, but nothing regenerates the samples.

```
0 3 * * *  pg_dump --format=custom twitchmetrics > /var/backups/tm-$(date +\%F).dump
```

`data/*.csv` is no longer the archive — it is a spool that is empty whenever the
database is reachable, so backing it up protects nothing. Back up the dump.

`data/.s3_buckets.json` is worth keeping too. It is not a secret, but it is the
only record of which random bucket name belongs to which channel; lose it and
`--setup` will make a second bucket rather than reusing the one already serving
that channel's history.

The token files are recoverable by re-authorizing, and should not be backed up
to anywhere less private than the server itself.
