"""Backing off and trying again, shared by every upload destination.

The policy is the same wherever the charts are published: a rate limit or a
5xx is worth another go after a pause, a permission error is not, and four
attempts is enough before admitting defeat. Only the way a given service spells
"slow down" differs, which is what `classify` is for.
"""

import secrets
import time
import urllib.error

from .logging import log

MAX_ATTEMPTS = 4


class GaveUp(RuntimeError):
    """Every attempt failed. The caller should log this and move to the next channel."""


def retry_after(attempt, header=None):
    """Backoff seconds: the service's own Retry-After, else 1, 2, 4, 8 with jitter."""
    if header:
        try:
            return max(1.0, float(str(header).strip()))
        except (TypeError, ValueError):
            pass
    # Jitter kept under half a second so the sequence stays monotonic.
    return 2.0 ** attempt + secrets.randbelow(500) / 1000.0


def with_backoff(what, call, classify=None):
    """Retry an idempotent operation through rate limits and 5xx, then give up.

    Wrap the whole find-then-write, never a bare mutating request: a create
    that timed out may well have succeeded server-side, and replaying its body
    would leave a duplicate behind. Re-running the existence check each attempt
    turns a partly-completed earlier one into a reuse.

    `classify(exc)` answers for the exceptions the destination understands,
    returning (retryable, retry_after_or_None). Returning None means "not mine",
    and the exception is re-raised untouched rather than retried blindly — the
    difference between a rate limit and a typo'd bucket name.
    """
    last = None
    for attempt in range(MAX_ATTEMPTS):
        hint = None
        try:
            return call()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Transport, not service: always worth another go.
            last = exc
            if attempt == MAX_ATTEMPTS - 1:
                raise
        except Exception as exc:  # noqa: BLE001 - classify() decides, not the type
            verdict = classify(exc) if classify else None
            if verdict is None:
                raise
            retryable, hint = verdict
            last = exc
            if not retryable or attempt == MAX_ATTEMPTS - 1:
                raise
        delay = retry_after(attempt, hint)
        log("WARN     {} failed ({}), retrying in {:.0f}s".format(what, last, delay))
        time.sleep(delay)
    raise GaveUp("{} gave up after {} attempts".format(what, MAX_ATTEMPTS))
