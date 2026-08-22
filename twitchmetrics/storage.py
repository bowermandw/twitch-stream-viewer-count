"""Reading and appending the sample CSVs.

Both pollers append and write the header only when the file is empty, so
stopping and restarting continues the same file with nothing overwritten.
"""

import csv
import os
from datetime import datetime, timezone

VIEWERS_HEADER = [
    "timestamp_utc", "is_live", "viewer_count",
    "title", "game", "started_at", "stream_id",
]

METRICS_HEADER = [
    "timestamp_utc", "is_live", "viewer_count", "follower_count", "chatter_count",
    "title", "game", "started_at", "stream_id",
]


def utc_stamp(when=None):
    return (when or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_row(path, header, row):
    """Append one row, writing the header only if the file is new or empty."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    need_header = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if need_header:
            writer.writerow(header)
        writer.writerow(row)


def write_all(path, header, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def read_samples(path):
    """Parse a viewers_*.csv or metrics_*.csv, skipping unreadable rows.

    Returns dicts with `followers` and `chatters` set to None when the file is
    the viewers-only format, so callers can treat both shapes alike.
    """
    samples = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                when = datetime.strptime(
                    row["timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except (ValueError, KeyError, TypeError):
                continue
            live = (row.get("is_live") or "").strip().lower() == "true"
            try:
                viewers = int(row["viewer_count"]) if live else None
            except (ValueError, KeyError, TypeError):
                continue

            def optional(name, _row=row):
                raw = (_row.get(name) or "").strip()
                try:
                    return int(raw) if raw else None
                except ValueError:
                    return None

            samples.append({
                "when": when,
                "live": live,
                "viewers": viewers,
                "followers": optional("follower_count"),
                "chatters": optional("chatter_count"),
                "title": row.get("title") or "",
                "game": row.get("game") or "",
                "stream_id": row.get("stream_id") or "",
            })
    samples.sort(key=lambda s: s["when"])
    return samples
