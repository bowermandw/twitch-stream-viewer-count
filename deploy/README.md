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

Two options:

**A. Authorize on your laptop, copy the token up.** The refresh token keeps it
alive indefinitely, so this is a one-time step:

```
# on your laptop
python3 -m twitchmetrics auth
scp data/.user_token.json server:/opt/twitch-metrics/data/
ssh server chmod 600 /opt/twitch-metrics/data/.user_token.json
```

**B. Forward the callback port over SSH** and authorize from the server:

```
ssh -L 3000:localhost:3000 server
cd /opt/twitch-metrics && python3 -m twitchmetrics auth --no-browser
# paste the printed URL into your local browser
```

**C. Skip it.** `--no-chatters` records viewers and followers only, and needs no
user token at all:

```
python3 -m twitchmetrics poll themeparkgiant --no-chatters
```

## Run it continuously

Copy `twitch-metrics.service` into systemd — see the comments at the top of that
file. Adjust `User`, `WorkingDirectory` and the channel.

```
sudo cp deploy/twitch-metrics.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now twitch-metrics
journalctl -u twitch-metrics -f
```

The poller handles token refresh, rate limits, API outages and network loss
itself, so `Restart=on-failure` is a backstop rather than the main mechanism.

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
