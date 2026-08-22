# Running on a server

## Requirements

**Python 3.9 or newer. Nothing else.**

There are no third-party Python packages — no `pip install` step, no virtualenv
needed, nothing to keep patched. The project uses only the standard library:
`urllib` for HTTP, `csv` for storage, and hand-built SVG for the charts.

```
python3 --version      # 3.9+
```

That's the whole dependency list. `requirements.txt` is intentionally empty and
says so.

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

If it won't start, `journalctl -u twitch-metrics@themeparkgiant -n 50` almost
always says why — usually a wrong `WorkingDirectory`, or a `User` that can't
read `.env`.

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

## Optional: PNG conversion

Charts are SVG, which any browser renders. If you want PNGs:

```
apt install librsvg2-bin           # Debian/Ubuntu
brew install librsvg               # macOS
rsvg-convert -w 1600 charts/chart_themeparkgiant_metrics.svg -o chart.png
```

Not required for anything the project does.

## Where things live

| Path | Contents |
|---|---|
| `data/` | sample CSVs, poll logs, cached tokens |
| `charts/` | generated SVGs |
| `.env` | credentials, mode 0600 |

Point them elsewhere with `TWITCH_DATA_DIR` and `TWITCH_CHARTS_DIR` if you'd
rather keep data on a mounted volume:

```
TWITCH_DATA_DIR=/var/lib/twitch-metrics python3 -m twitchmetrics poll
```

## Backups

`data/*.csv` is the only irreplaceable thing here — charts regenerate from it.
The token files are recoverable by re-authorizing, and should not be backed up
to anywhere less private than the server itself.
