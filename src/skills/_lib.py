"""Shared helpers for RunAI skills.

Files whose name starts with "_" are NOT registered as skills, but they can be
imported by skills (the skills dir is on sys.path). Put reusable plumbing here.
"""
import json
import os
import urllib.error
import urllib.request

SECRETS = os.path.expanduser("~/.runai/secrets.json")


def secret(key, default=None):
    """Read a value from ~/.runai/secrets.json (tokens, account ids, etc.)."""
    try:
        with open(SECRETS) as f:
            return json.load(f).get(key, default) or default
    except Exception:
        return default


def http(method, url, headers=None, body=None, timeout=30):
    """HTTP request. A dict `body` is sent as JSON. Returns parsed JSON or raw text.
    On an HTTP error it returns {"_status": code, "_error": text}."""
    h = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return {"_status": e.code, "_error": e.read().decode("utf-8", "ignore")[:1000]}
    except Exception as e:
        return {"_error": str(e)}
    try:
        return json.loads(raw)
    except Exception:
        return raw


def confirm(prompt):
    """Ask the user y/N at the REPL (used to gate destructive skill actions).
    In web mode the cockpit approval UI handles confirmation, so skip input()."""
    if os.environ.get("RUNAI_WEB_MODE"):
        return True
    try:
        return input(f"  {prompt} [y/N] ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False
