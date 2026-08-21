# Twitch Viewer Count Poller

Records the live viewer count for a Twitch channel into a CSV file on a fixed
interval, then charts it in the style of the YouTube Studio "Concurrent
viewers" graph.

**Python 3, standard library only — nothing to install.** No matplotlib, no
numpy, no `pip install`.

![Example chart](docs_chart_testchannel.png)

*Generated with `python3 graph.py testchannel` from the sample data committed to
this repo. The header carries the peak, **when** the peak happened, and the
average; the amber segments are per-30-minute averages.*

### What it does

- Polls the Twitch Helix API on an interval and appends one CSV row per sample
- Records offline polls too, so a gap in the data means "not running" rather than "not streaming"
- Handles token refresh, rate limits, outages and network loss without dying
- Keeps each channel in its own files, so several can be polled at once
- Charts any broadcast in the file as a self-contained SVG
- Ships synthetic sample data so you can try the chart before collecting anything

## Setup

Run this and follow the prompts:

```
python3 setup.py
```

It prints the instructions below, offers to open the Twitch dev console in your
browser, asks for the two values, **verifies them against the real API**, and
writes `.env` for you. If Twitch rejects them, nothing is written and it tells
you why — so you can't end up with a silently broken config.

### What you'll do in the browser

Twitch has no API for creating an app or reading back a secret, so this part is
manual — once, about two minutes.

1. Go to <https://dev.twitch.tv/console/apps/create> and log in.
   Twitch requires 2FA on the account to use the dev console; if you don't have
   it enabled it will make you set it up first (Settings -> Security).

2. Fill in the form:

   | Field | Value |
   |---|---|
   | **Name** | Anything unique across all of Twitch, e.g. `ign-viewer-log`. If it says the name is taken, add a number. |
   | **OAuth Redirect URLs** | `http://localhost:3000` — a required field, but this app never uses it. There's no browser redirect in the flow we use. |
   | **Category** | `Analytics Tool` |
   | **Client Type** | `Confidential` |

3. Click **Create**. You're back at the app list.

4. Click **Manage** next to your new app.

5. **Client ID** is on that page — copy it.

6. Click **New Secret**, confirm, and copy the **Client Secret** immediately.
   It is shown only once. If you lose it, click New Secret again — note that
   this invalidates the previous secret.

Then paste both into the `setup.py` prompt. The secret is typed hidden and is
never echoed to your terminal scrollback.

### Doing it by hand instead

If you'd rather skip the script, copy the template and fill it in:

```
cp .env.example .env
```

Or pass the values directly:

```
python3 setup.py --client-id XXXX --client-secret YYYY
```

No user login or OAuth scopes are needed — live stream data is public, so this
uses the server-to-server client credentials grant.

## Usage

The channel is the first argument. Each channel keeps its own data files, so
you can poll several at once from separate terminals.

```
python3 twitch_viewers.py ign   # poll ign
python3 twitch_viewers.py       # poll the default channel
```

`Ctrl-C` to stop. The terminal must stay open for polling to continue.

A single poll, useful for checking things work:

```
python3 twitch_viewers.py ign --once
```

`--channel ign` also works if you prefer the flag form.

### Setting a default channel

Without an argument it polls `DEFAULT_CHANNEL` from the top of
`twitch_viewers.py`. To change that without editing code, add to `.env`:

```
TWITCH_CHANNEL=ign
```

Precedence: **command line > `TWITCH_CHANNEL` > `DEFAULT_CHANNEL`**.

### Polling interval

`INTERVAL_SECONDS` at the top of `twitch_viewers.py` (currently 300 = 5
minutes). Polls land on wall-clock boundaries, so 300 gives you :00, :05, :10.
Twitch's rate limit is 800 points/minute — an enormous amount of headroom, so
polling more often is fine.

## Files

| File | Purpose |
|---|---|
| `setup.py` | One-time credential setup and verification |
| `twitch_viewers.py` | The poller |
| `graph.py` | Charts the collected data as an SVG |
| `make_test_data.py` | Generates realistic fake data for testing the chart |
| `.env` | Your credentials (gitignored, mode 0600) |
| `viewers_<channel>.csv` | Collected data, one file per channel |
| `poll_<channel>.log` | Status line history, one file per channel |
| `chart_<channel>.svg` | Generated chart |
| `.token_cache.json` | Cached app token, shared across channels |

Channel names are lowercased for filenames, so `ign` write to the
same `viewers_ign.csv` rather than splitting your data across two files.

## Output

**`viewers_<channel>.csv`** — one row per poll:

```
timestamp_utc,is_live,viewer_count,title,game,started_at,stream_id
2026-08-21T17:09:52Z,true,176,CODNext Showcase 2026,Special Events,2026-08-21T15:28:54Z,318926150999
2026-08-21T17:14:52Z,true,181,CODNext Showcase 2026,Special Events,2026-08-21T15:28:54Z,318926150999
2026-08-21T17:19:52Z,false,,,,,
```

Offline polls are recorded as rows with `is_live=false`, so a *gap* in the data
means the script wasn't running, not that the channel was off.

`stream_id` changes between broadcasts — use it to group samples into distinct
stream sessions.

**`poll_<channel>.log`** — the same status lines printed to the console, for
checking on a long run after the fact.

## Graphing

```
python3 graph.py IGN --open
```

Reads `viewers_<channel>.csv` and writes `chart_<channel>.svg`, styled after the
YouTube Studio "Concurrent viewers" chart: peak and average in the header, a
filled area curve, gridlines, and elapsed time along the bottom.

It also prints a text summary with a per-block breakdown, so you get the numbers
without opening the chart.

Pure standard library — no matplotlib, no numpy. SVG opens in any browser and
stays sharp at any size.

### Peak timing

The header shows **when** the peak happened, both as elapsed stream time and as
a wall-clock time (`at 3:10:00 · 4:00 AM`). The moment is also marked on the
curve itself with a dot and a dashed drop-line.

### Block averages

Instead of one average line across the whole chart, a short amber line sits over
each block at that block's average, with a faint vertical divider at each
boundary — so you can see how the audience moved through the stream rather than
just its overall level.

```
python3 graph.py IGN                 # 30-minute blocks (default)
python3 graph.py IGN --bucket 60     # hourly
python3 graph.py IGN --bucket 15     # finer
python3 graph.py IGN --no-buckets    # just the curve
```

Hourly blocks on the same data:

![Hourly averages](docs_chart_hourly.png)

### Picking a broadcast

A CSV accumulates every broadcast. `graph.py` splits them on offline rows,
`stream_id` changes, and gaps where the poller wasn't running, then charts the
most recent one.

```
python3 graph.py IGN --list-sessions   # see them all
python3 graph.py IGN --session 0       # chart an earlier one
```

Other options: `--output PATH`, `--width`, `--height`, and a direct path
(`python3 graph.py some_file.csv`).

## Test data

To design or check the chart without waiting for a real 8-hour stream:

```
python3 make_test_data.py testchannel
python3 graph.py testchannel --open
```

`viewers_testchannel.csv` and `chart_testchannel.svg` are committed, so the
chart above can be reproduced without running the poller at all. The generator
writes the same columns the poller produces, so `graph.py` can't tell the
difference. The curve is shaped like a real broadcast
— a ramp at the start, a mid-stream bump, correlated jitter rather than random
static, occasional spikes, a slow decline and a sharp drop at the end. It also
writes a short earlier broadcast plus offline rows, so session-splitting gets
exercised.

```
python3 make_test_data.py testchannel --hours 4 --peak 2000 --interval 60
python3 make_test_data.py testchannel --single --seed 42
```

The seed is fixed by default, so regenerating gives identical data.

## Notes

- `viewer_count` is Twitch's live concurrent-viewer number and lags reality by
  about a minute. Don't confuse it with `view_count` on the Get Users endpoint,
  which is a deprecated lifetime-views field that now always returns 0.
- The 10-minute interval is the `INTERVAL_SECONDS` constant at the top of
  `twitch_viewers.py`. Twitch's rate limit (800 points/min) leaves enormous
  headroom if you want to poll more often.
- Network errors, Twitch outages and rate limits are logged and skipped; the
  loop keeps running. An expired or revoked token is refreshed automatically.
