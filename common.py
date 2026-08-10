#!/usr/bin/env python3
"""Shared plumbing for watch.py, approve.py and publish.py.

Stdlib only, same constraint as the scripts that import it: the workflows run
these with a bare `python x.py` and no install step.
"""
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def die(msg, code=1):
    print(f"\n\u2717 {msg}\n", file=sys.stderr)
    sys.exit(code)


def utf8_stdout():
    """Windows consoles default to cp1252 and choke on the emoji these scripts
    print."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def require_env(name, hint=""):
    val = os.environ.get(name, "").strip()
    if not val:
        die(f"{name} is not set." + (f"\n  {hint}" if hint else ""))
    return val


def read_json(path, default=None):
    """Parse a JSON file, `default` when it does not exist. A corrupt file is
    fatal and says which file it is, rather than surfacing a bare traceback."""
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        die(f"{path} is not valid JSON ({e}). Fix or delete it.")


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1), encoding="utf-8")


def request_json(url, data=None, headers=None, timeout=30):
    """One HTTP call returning parsed JSON. POSTs when `data` is given.

    HTTPError/URLError are left to the caller: each API's failures need their
    own explanation (an expired IG token reads nothing like a bad chat id).
    """
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def multipart(fields, files):
    """Encode form fields plus file uploads. Returns (content_type, body)."""
    boundary = uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
        ).encode()
    for field, path in files.items():
        path = Path(path)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}";'
            f' filename="{path.name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode()
        body += path.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", bytes(body)


def telegram_token():
    return require_env(
        "TELEGRAM_BOT_TOKEN",
        "Get one from @BotFather and add it as a repository secret (SETUP.md step 1).",
    )


def telegram_chat_id():
    return require_env(
        "TELEGRAM_CHAT_ID",
        "The numeric chat id from api.telegram.org/bot<token>/getUpdates "
        "(SETUP.md step 2).",
    )


def telegram(method, params=None, files=None, timeout=120):
    """POST to the Bot API. Uses multipart only when uploading a local file."""
    url = TELEGRAM_API.format(token=telegram_token(), method=method)
    params = {k: v for k, v in (params or {}).items() if v is not None}

    if files:
        ctype, body = multipart(params, files)
        headers = {"Content-Type": ctype}
    else:
        body = urllib.parse.urlencode(params).encode()
        headers = {}

    try:
        return request_json(url, data=body, headers=headers, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        die(f"Telegram {method} failed: {e.code} {detail}")
