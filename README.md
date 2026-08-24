# Twitch Metrics

Poll a Twitch channel's **viewers, followers and chat size** on an interval into
a CSV, then chart it in the style of the YouTube Studio "Concurrent viewers"
graph.

**Python 3.9+ and nothing else.** No dependencies, no `pip install`, no
virtualenv needed — only the standard library. `urllib` for HTTP, `csv` for
storage, hand-built SVG for the charts.

That covers polling, charting and the Google Drive upload — Drive is plain REST
and its OAuth is plain form posts, so `urllib` handles both. The one addition is
the [daily report](#daily-report), which converts its charts to PNG and so wants
the `rsvg-convert` binary (`apt install librsvg2-bin`) — a system package, not a
Python dependency.

![Example chart](docs/chart_testchannel.png)

Polling all three metrics gives a panel each:

![All three metrics](docs/chart_metrics.png)

*Both generated from the synthetic fixtures in `tests/fixtures/`, so they
reproduce without credentials.*

### What it does

- Samples viewers, followers and chat size on one tick into `data/metrics_<channel>.csv`
- Records offline polls too, so a gap in the data means "not running" rather than "not streaming"
- Handles token refresh, rate limits, API outages and network loss without dying
- Charts a single broadcast or a whole calendar day, one metric or all three
- Degrades cleanly when a metric needs permissions you don't have
- Ships synthetic sample data so the charts work before you collect anything
- Uploads a PNG per channel to your own Google Drive once a day, on a timer

---

## Install

```
git clone https://github.com/bowermandw/twitch-stream-viewer-count.git
cd twitch-stream-viewer-count
python3 -m twitchmetrics --help
```

That's it — there is nothing to install, and no virtualenv is needed to run it.

Optionally put a `twitch-metrics` command on your PATH. Note that modern Pythons
(Homebrew, Debian, Ubuntu) are *externally managed* and will refuse a bare
`pip install`, so use a venv or pipx:

```
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/twitch-metrics --help
```

```
pipx install .          # if you have pipx
```

Either way **no dependencies are downloaded** — the install only creates the
entry point. If you'd rather not bother, `python3 -m twitchmetrics` is
equivalent everywhere and needs nothing. This README writes the short form for
readability; substitute whichever you use.

For servers, see [`deploy/README.md`](deploy/README.md) — requirements, running
it in the background, and how to handle the one step that needs a browser.

## Credentials

```
twitch-metrics setup
```

Walks you through registering a Twitch app, then **verifies the credentials
against the live API** before writing `.env` at mode 0600. If Twitch rejects
them, nothing is written and it tells you why.

<details>
<summary>What you'll do in the browser (about 2 minutes, once)</summary>

Twitch has no API for creating an app or reading back a secret, so this part is
manual.

1. Go to <https://dev.twitch.tv/console/apps/create> and log in.
   The dev console requires 2FA on the account; if you don't have it enabled it
   will make you set it up first.

2. Fill in the form:

   | Field | Value |
   |---|---|
   | **Name** | Anything unique across Twitch, e.g. `themeparkgiant-viewer-log` |
   | **OAuth Redirect URLs** | `http://localhost:3000` — required, and used only by `twitch-metrics auth` |
   | **Category** | `Analytics Tool` |
   | **Client Type** | `Confidential` |

3. Click **Create**, then **Manage** on the new app.

4. Copy the **Client ID**. Click **New Secret** and copy that too — it is shown
   only once, and generating another invalidates the previous one.

Non-interactive, for servers:

```
twitch-metrics setup --client-id XXXX --client-secret YYYY
```
</details>

---

## Polling

```
twitch-metrics poll themeparkgiant
```

```
themeparkgiant  LIVE     viewers      51  followers      751  chat     4
```

One row per tick in `data/metrics_<channel>.csv`:

```
timestamp_utc,is_live,viewer_count,follower_count,chatter_count,title,game,started_at,stream_id
```

Followers and chat size are recorded **even when the channel is offline** —
people follow and bots sit in chat between streams — while `viewer_count` is
blank. Only `is_live` marks a broadcast.

```
twitch-metrics poll themeparkgiant --once           # one sample
twitch-metrics poll themeparkgiant --no-chatters    # skip chat size
twitch-metrics poll themeparkgiant --viewers-only   # viewers alone
twitch-metrics poll themeparkgiant --interval 60    # every minute
```

### Changing the interval

No code edit needed. Either flag it per run, or set it once in `.env`:

```
TWITCH_INTERVAL=60
```

Precedence is **`--interval` > `TWITCH_INTERVAL` > 300 seconds**, with a minimum
of 10. A value that isn't a number is reported rather than silently ignored,
since a typo in a service file would otherwise poll at the wrong rate unnoticed.

Samples land on wall-clock boundaries, so 60 gives you :00, :01, :02 and 300
gives :00, :05, :10.

Twitch's rate limit is 800 points per minute. Polling three metrics every 60
seconds for one channel uses 3 — even a dozen channels at that rate is nowhere
near it. The real cost is file size: one channel at 60s writes about 1,440 rows
a day, roughly 100 MB a year, against 20 MB at the default.

### Running it in the background

`Ctrl-C`, `SIGTERM` and `SIGHUP` all shut down cleanly — the poller finishes the
current sample, writes a summary and exits 0, without waiting out the rest of
the interval. That makes it safe under a service manager.

```
tmux new -s twitch                                    # quick: Ctrl-B D to detach
sudo systemctl enable --now twitch-metrics@themeparkgiant   # permanent
```

### Several channels at once

Each channel keeps its own CSV and log, so pollers run side by side without
interfering. The systemd unit is a template — the name after the `@` is the
channel:

```
sudo systemctl enable --now twitch-metrics@themeparkgiant
sudo systemctl enable --now twitch-metrics@prgskidmark
```

The only shared state is the cached tokens. Those are written atomically, and
refreshes are serialised with a file lock, because Twitch rotates the refresh
token on use.

The unit and recipes for tmux, nohup, cron, launchd and logrotate are in
[`deploy/`](deploy/).

### What each metric needs

| Metric | Token | Works for |
|---|---|---|
| Viewers | app | any channel |
| Followers | app | any channel |
| Chat size | user + `moderator:read:chatters` | channels you moderate |

Chat size is the only one needing a browser login, so it degrades rather than
blocking: without a usable token, or on a channel you don't moderate, the column
is left blank and the other two carry on. After three consecutive failures it
stops asking, so a permission change mid-run doesn't fill the log with errors.

### The CSV always appends

Both formats open in append mode and write the header only when the file is
empty, so stopping and restarting — minutes or days later — continues the same
file. Nothing is overwritten, and no marker is written at the join.

A gap in the timestamps is therefore the only record that polling stopped.
`graph` infers one when the interval exceeds 2.5× the median and treats the
samples either side as separate broadcasts. `--date` ignores that and takes the
whole day.

---

## Graphing

```
twitch-metrics graph themeparkgiant --open
```

Reads `data/metrics_<channel>.csv` (falling back to `viewers_<channel>.csv`) and
writes `charts/chart_<channel>_metrics.svg`. It also prints a text summary, so
you get the numbers without opening anything.

### Peak timing

Every peak reports **when** it happened — wall-clock first, elapsed second
(`peak 740 at 4:45 AM (3:55:00 into the stream)`) — in the header, the panel
captions and the text summary. The moment is marked on the curve with a dot and
a dashed drop-line.

Follower tiles show no time, because that number is the change across the window
rather than a moment.

### Block averages

Instead of one average line across the whole chart, a short line sits over each
block at that block's average, with a faint divider at each boundary.

```
twitch-metrics graph testchannel               # 30-minute blocks (default)
twitch-metrics graph testchannel --bucket 60   # hourly
twitch-metrics graph testchannel --no-buckets  # just the curve
```

![Hourly averages](docs/chart_hourly.png)

### One metric, or all three at once

Each panel gets its own axis, because the three live on completely different
scales. Followers deliberately **do not** use a zero-based axis: on a 0–800
scale, a 40-follower gain is an invisible flat line. The panel spans the actual
range instead.

`--composite` overlays all three, each normalised to its own range with the real
range in the legend:

![Composite](docs/chart_composite.png)

```
twitch-metrics graph testchannel --composite
twitch-metrics graph testchannel --only chatters
twitch-metrics graph testchannel --viewers-only
```

### Charting one day

```
twitch-metrics graph themeparkgiant --date 2026-08-22
twitch-metrics graph themeparkgiant --date today
twitch-metrics graph themeparkgiant --list-days
```

This charts a **calendar day** rather than a single broadcast: from the first
live sample of that day to the last, keeping any offline stretch in between so
the day reads continuously.

![One day with breaks](docs/chart_day.png)

That day was three sittings with two breaks. Three things to notice:

- The **viewers** line breaks over each gap rather than drawing a straight
  segment across it, because there is genuinely no viewer count while offline.
- **Followers** run unbroken straight through — people follow between streams,
  and that is real data.
- Offline stretches are **shaded**, and the x-axis switches to clock times.

Dates match in **local time**, which is the day you mean when you type one, even
though the CSV stores UTC.

Without `--date`, the same file splits at each break:

```
$ twitch-metrics graph breaktest --list-sessions
4 broadcast(s) in metrics_breaktest.csv:
  [0] Tue 18 Aug 5:05 AM    0:45:00  peak    180  avg    109  (10 samples)
  [1] Wed 19 Aug 12:50 AM    2:30:00  peak    740  avg    528  (31 samples)
  [2] Wed 19 Aug 4:00 AM    2:30:00  peak    740  avg    464  (31 samples)
  [3] Wed 19 Aug 6:55 AM    3:00:00  peak    740  avg    538  (37 samples)

$ twitch-metrics graph breaktest --list-days
2 day(s) with live data in metrics_breaktest.csv:
  2026-08-18    0:45:00  peak    180  10 samples
  2026-08-19    9:05:00  peak    740  112 samples   (0:55:00 offline mid-day)
```

Use `--session` for one sitting, `--date` for a whole day. They can't be
combined, since they select different things.

---

## Daily report

One command charts every channel you are polling, converts each to PNG and
uploads them to your own Google Drive. A systemd timer runs it at 17:00; see
[`deploy/`](deploy/).

```
twitch-metrics daily                          # today, every polled channel
twitch-metrics daily --dry-run                # render and convert, no upload
twitch-metrics daily --date yesterday         # backfill a day
twitch-metrics daily themeparkgiant           # just this one
twitch-metrics daily --list-channels          # who's in, and what today looks like
```

```
[…] start    daily report for 2026-08-23 — 3 channel(s) from the enabled systemd units
[…] themeparkgiant  287 KB -> chart_themeparkgiant_2026-08-23.png
[…] themeparkgiant  uploaded to Twitch Metrics/themeparkgiant/2026-08-23.png
[…] skip     prgskidmark — offline all day, nothing to chart
[…] stop     1 uploaded, 1 dark in 6.4s
```

Files land at `Twitch Metrics/<channel>/<YYYY-MM-DD>.png`, one folder per
channel. Re-running the same day **replaces** that file rather than adding a
second copy — Drive allows duplicate names, so the upload looks for the name
first and patches the bytes if it is already there. The link stays stable and
Drive keeps the earlier render as a revision.

PNG rather than SVG because Drive previews PNG properly and mostly offers an SVG
as a download, which is no use on a phone. Conversion is `rsvg-convert`, and the
job **fails at startup** if it isn't installed rather than rendering everything
first and then failing per file.

### Which channels

The list comes from the pollers you have enabled, so adding a channel to the
report is just `systemctl enable twitch-metrics@thatchannel`. In precedence
order:

| Source | |
|---|---|
| positional arguments, or `--channel` (repeatable) | `daily a b` |
| `TWITCH_DAILY_CHANNELS` in the environment or `.env` | `a,b` or `a b` |
| enabled `twitch-metrics@*` systemd instances | the normal case |
| `systemctl list-units` | fallback: started but not enabled |
| nothing found | **exits 1** — a daily job going quiet is not success |

Enabled rather than *running* on purpose. A channel you enabled whose poller
died at 03:00 is exactly what the report should shout about; ask "what's
running?" and that channel drops off the list and the job goes green, which
disables the alarm at the moment it matters.

### A day off is not a failure

The poller records followers even when a channel is offline, so "didn't stream"
and "poller wasn't running" look similar in the CSV. They are told apart,
because otherwise every day you take off turns `systemctl status` red and within
a fortnight nobody reads it:

| The CSV for that day | Means | Result |
|---|---|---|
| has live samples | there's a chart to draw | rendered and uploaded |
| has rows, none live | you didn't stream | **skipped, exit 0** |
| has no rows at all | the poller wasn't running | **failure, exit 1** |
| doesn't exist | never polled, or the wrong `TWITCH_DATA_DIR` | **failure, exit 1** |

Exit codes are 0 and 1 only. A failure on one channel doesn't stop the others —
every channel is attempted, and the last log line is always the summary.

## Google Drive

```
twitch-metrics drive --setup     # register an OAuth client, verified before saving
twitch-metrics drive --auth      # browser login, once
twitch-metrics drive --status    # which account, which scopes, time left
twitch-metrics drive --check     # resolve the real target folder, as the service will
twitch-metrics drive --revoke    # revoke and delete the token
```

`--setup` walks through creating a Google Cloud project, enabling the Drive API
and making a **Desktop app** OAuth client. Uploads go to *your* My Drive and are
owned by you; there is no service account involved.

The scope is `drive.file` — per-file access, so this tool can see only the files
and folders it created itself, never the rest of your Drive. That is also why
you should let it create the `Twitch Metrics` folder rather than making one by
hand: a folder it didn't create is invisible to it, and it would make a second
one alongside. Renaming or moving the folder afterwards is fine — the folder id
is remembered, so the uploads follow it.

> **The one trap worth knowing.** If you leave the OAuth consent screen in
> "Testing", Google expires refresh tokens after **7 days** — the daily upload
> works all week and then stops. Click **Publish app** so it reads "In
> production". An app requesting only `drive.file` needs no security review; you
> just get a one-time "Google hasn't verified this app" screen, where you click
> Advanced and continue. `drive --status` warns as the week runs out, and the
> failure itself says what to do.

Authorizing from a server works the same way as `auth` — an
[SSH tunnel](#on-a-machine-with-no-browser) or `--manual`. Both flows listen on
port 3000, so they can't run at the same moment; `GOOGLE_REDIRECT_URI` moves one
if you need to.

---

## Other lookups

Each takes the channel as its first argument, falling back to `TWITCH_CHANNEL`
then the built-in default, and accepts a login, an `@handle`, or a numeric ID.

### Accounts

```
twitch-metrics users prgskidmark
twitch-metrics users ign prgskidmark themeparkgiant   # batched, up to 100
twitch-metrics users 35616747 --by-id
```

Twitch omits unknown logins rather than erroring, so the command reports which
of the names you asked for came back empty.

`view_count` is still returned by this endpoint but always contains `0` — Twitch
retired lifetime view counts in 2022 and dropped the field from their docs
without removing it from the API. It's labelled deprecated rather than shown as
if it meant something.

### Followers

```
twitch-metrics followers themeparkgiant                 # just the count
twitch-metrics followers themeparkgiant --recent 10     # newest, with how long ago
twitch-metrics followers themeparkgiant --list          # everyone, paged
twitch-metrics followers themeparkgiant --check someone # do they follow, and since when
```

The count uses the app token and works for **any** channel:

```
$ twitch-metrics followers ign
IGN — 308,332 followers
```

Seeing *who* follows is different — Twitch returns `total` to anyone but
withholds the `data` array unless the token belongs to the broadcaster or a
moderator and carries `moderator:read:followers`.

### Chatters

```
twitch-metrics chatters themeparkgiant
twitch-metrics chatters themeparkgiant --list
```

```
themeparkgiant — 3 people in chat
  (as moderator prgskidmark)
```

You don't pass a moderator ID: Twitch requires it to match the token's own user,
so it's read from the token rather than left as something to get wrong.

The count includes bots and includes whoever is making the request, so it has a
floor rather than reaching zero.

---

## User authorization

Most commands use an **app access token** (client credentials), which represents
the application and needs no login. Chat size and follower *names* need a **user
access token** representing a person:

```
twitch-metrics auth
```

Opens Twitch, you approve, and it catches the redirect on `http://localhost:3000`
— the redirect URL registered during setup. Sign in as the account that
moderates the channel.

```
twitch-metrics auth --status    # who it's for, scopes, time left
twitch-metrics auth --force     # log in again, e.g. as someone else
twitch-metrics auth --revoke    # revoke and delete it
twitch-metrics auth --manual    # headless: paste the code back
```

### On a machine with no browser

Authorizing from a server needs one extra step, because the redirect to
`http://localhost:3000` resolves to whichever machine the *browser* is on.

**SSH tunnel** — forwards your desktop's port 3000 to the server's, so the
normal flow works unchanged. On your desktop:

```
ssh -L 3000:localhost:3000 user@your-server
```

Leave that open, and in it run:

```
python3 -m twitchmetrics auth --no-browser
```

Paste the printed URL into your desktop browser, approve, and the redirect
travels back down the tunnel to the waiting listener. The tunnel is only needed
for this one step.

**Or skip the tunnel** with `--manual`, which prints the URL and waits for you
to paste the redirect back:

```
twitch-metrics auth --manual
```

The browser will fail to reach `localhost:3000` — expected — but the address bar
still holds `?code=...`. Paste that whole URL at the prompt. State is verified,
so the CSRF check survives the detour.

Full server notes, including copying an existing token up instead, are in
[`deploy/README.md`](deploy/README.md).

The token lasts about four hours and refreshes itself from the stored refresh
token, so the browser step happens once. Requesting a new scope carries the
already-granted ones along, so adding one doesn't break another command.

---

## Test data

To work on the charts without waiting for a real 8-hour stream:

```
twitch-metrics testdata testchannel
twitch-metrics graph testchannel --open
```

The three metrics are generated as one **correlated** system rather than
independently: chat size tracks viewers above a bot floor, and followers accrue
faster while more people are watching, with occasional unfollows so the line
isn't suspiciously monotonic. The viewer curve ramps, bumps mid-stream, jitters
with correlated rather than random noise, declines slowly and drops sharply at
the end.

```
twitch-metrics testdata testchannel --hours 4 --peak 2000 --interval 60
twitch-metrics testdata breaktest --break 2.5:40 --break 5:25
```

`--break H:M` inserts an offline gap M minutes long, H hours in — repeatable,
each restart taking a new `stream_id` the way Twitch issues one. That's what the
day chart above is generated from. The seed is fixed by default, so regenerating
gives identical data.

## Tests

```
python3 tests/smoke.py
```

199 checks over the committed fixtures — parsing, session detection, day
selection, gap handling, axis choice, path safety, rendering, CLI wiring,
channel discovery, the SVG-to-PNG step, and the Drive query and multipart
builders. No network, no credentials, no tokens. It won't catch Twitch changing an API
contract; only regressions in this code.

---

## Layout

```
twitchmetrics/          the package
  config.py             paths, .env, channel resolution
  auth.py               app access token
  useroauth.py          user access token (browser flow)
  api.py                Helix endpoint wrappers
  storage.py            CSV read and append
  chart.py              SVG rendering
  png.py                SVG to PNG, via rsvg-convert
  driveoauth.py         Google access token (browser flow)
  drive.py              Drive v3 endpoint wrappers
  testdata.py           synthetic data model
  cli.py                subcommand dispatch
  commands/             one module per subcommand
data/                   samples, logs, cached tokens   (gitignored)
charts/                 generated SVGs                 (gitignored)
tests/fixtures/         synthetic sample data           (committed)
docs/                   README images
deploy/                 systemd units and server notes
```

`data/` and `charts/` can be redirected with `TWITCH_DATA_DIR` and
`TWITCH_CHARTS_DIR`, which is useful when the data belongs on a mounted volume.

`data/` holds the only irreplaceable thing here — charts regenerate from the
CSVs, and tokens can be re-fetched.

## Notes

- `viewer_count` is Twitch's live concurrent-viewer number and lags reality by
  about a minute. Treat it as approximate.
- Twitch's rate limit is 800 points/minute. Polling every 5 minutes uses a
  vanishing fraction of that, so a shorter interval is fine.
- Charts are SVG. To convert one by hand: `brew install librsvg` or
  `apt install librsvg2-bin`, then `rsvg-convert -w 1600 in.svg -o out.png`.
  That binary is the one thing `daily` needs installed, because it uploads PNGs;
  nothing else here uses it.
- On a minimal Debian with no fonts installed, `rsvg-convert` exits 0 and writes
  a PNG with the text missing. `apt install fonts-dejavu-core`, and eyeball the
  first upload once — no test can catch that.
