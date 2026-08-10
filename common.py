#!/usr/bin/env python3
"""Shared plumbing for the pipeline scripts. Stdlib only, same as everything else.

Three things every script here got wrong on its own:

* **A 200 is not a success.** Both Telegram and the Graph API answer some
  failures with HTTP 200 and an error body. Reading only `result` turns those
  into a `None` that travels for hours before it breaks something -- a queue
  entry with `message_id: null` can never have its buttons retired.
* **A dropped connection is not a crash.** Feeds, Telegram and Instagram all
  fail transiently. One 502 should cost a retry, not a red workflow run.
* **State must survive a half-finished write.** `state/queue.json` is the only
  record of what already published. A crash during `write_text` truncates it,
  and the next run re-posts everything it can no longer see.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

# Transient by nature: retrying costs a few seconds, giving up costs the run.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRIES = 3
BACKOFF = 2.0


class PipelineError(Exception):
    """Anything this pipeline can explain to the user without a traceback."""


class HttpError(PipelineError):
    def __init__(self, url, status, body):
        self.url = url
        self.status = status
        self.body = body
        super().__init__(f"{url} -> HTTP {status}\n  {body.strip()[:600]}")


class ApiError(PipelineError):
    """The transport succeeded and the service still said no."""

    def __init__(self, url, payload):
        self.url = url
        self.payload = payload
        super().__init__(f"{url} rejected the request\n  {payload}")


def fail(msg, code=1):
    """Exit with an explanation instead of a traceback."""
    print(f"\n\u2717 {msg}\n", file=sys.stderr)
    raise SystemExit(code)


def warn(msg):
    print(f"  ! {msg}", file=sys.stderr)


def request_json(req, timeout=30, retries=RETRIES, label=None):
    """Perform `req` and decode the JSON body.

    Raises HttpError for a non-2xx the service meant, PipelineError for a
    network failure or an undecodable body. Retries only what is worth
    retrying, so a 401 fails immediately and a 502 does not.
    """
    label = label or req.full_url
    last = None

    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            last = HttpError(label, e.code, body)
            if e.code not in RETRY_STATUSES:
                raise last
        except (urllib.error.URLError, OSError) as e:
            # socket.timeout is an OSError and does not always arrive wrapped
            # in URLError, so both have to be caught here.
            reason = getattr(e, "reason", e)
            last = PipelineError(f"could not reach {label}: {reason}")

        if attempt == retries:
            raise last
        sleep = BACKOFF ** attempt
        warn(f"{label}: {last} — attempt {attempt} of {retries}, "
             f"retrying in {sleep:.0f}s")
        time.sleep(sleep)

    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise PipelineError(
            f"{label} returned something that is not JSON ({e})\n"
            f"  {raw[:300]!r}"
        ) from e


def read_json(path, default=None, what="file"):
    """Read JSON, or `default` when the file is absent.

    A *corrupt* file is never treated as an absent one: `state/queue.json`
    holding verdicts and published markers is the closest thing to a database
    here, and quietly starting from `{}` would re-publish everything in it.
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise PipelineError(
            f"{path} is not valid JSON ({e}).\n"
            f"  This {what} is state the pipeline relies on — inspect it and "
            f"fix or delete it deliberately."
        ) from e
    except OSError as e:
        raise PipelineError(f"could not read {path}: {e}") from e


def write_json(path, obj):
    """Write JSON atomically: a crash mid-write must not truncate state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise PipelineError(f"could not write {path}: {e}") from e
