"""Shared helpers: settings, Supabase REST/Storage and plain HTTP (stdlib only)."""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env():
    """Load guest-radar/.env into os.environ (real environment variables win)."""
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if v.strip() and not os.environ.get(k.strip()):
                os.environ[k.strip()] = v.strip()


def set_env_value(key, value):
    """Write or replace KEY=value in guest-radar/.env."""
    env = ROOT / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    out, found = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    env.write_text("\n".join(out) + "\n")
    os.environ[key] = value


def http(method, url, headers=None, body=None, raw=False, timeout=120):
    """Small JSON-over-HTTP helper. Returns parsed JSON (or bytes if raw)."""
    data = None
    headers = dict(headers or {})
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        else:
            data = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
    headers.setdefault("User-Agent", "guest-radar/1.0")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:800]
        raise RuntimeError(f"{method} {url.split('?')[0]} -> {e.code}: {detail}") from None
    if raw:
        return payload
    if not payload:
        return None
    try:
        return json.loads(payload)
    except ValueError:
        return payload.decode(errors="replace")


class Supabase:
    """Service-role access to the Guest Radar database and storage."""

    def __init__(self, url=None, key=None):
        self.url = (url or os.environ["SUPABASE_URL"]).rstrip("/")
        self.key = key or os.environ["SUPABASE_SERVICE_KEY"]

    def _h(self, extra=None):
        h = {"apikey": self.key}
        if not self.key.startswith("sb_"):  # legacy JWT keys also go in Authorization
            h["Authorization"] = f"Bearer {self.key}"
        h.update(extra or {})
        return h

    def select(self, table, query="select=*"):
        return http("GET", f"{self.url}/rest/v1/{table}?{query}", self._h())

    def insert(self, table, rows, upsert=False):
        prefer = "return=representation"
        if upsert:
            prefer += ",resolution=merge-duplicates"
        return http("POST", f"{self.url}/rest/v1/{table}", self._h({"Prefer": prefer}), rows)

    def delete(self, table, query):
        return http("DELETE", f"{self.url}/rest/v1/{table}?{query}", self._h())

    def upload(self, bucket, path, data, content_type):
        http(
            "POST",
            f"{self.url}/storage/v1/object/{bucket}/{path}",
            self._h({"Content-Type": content_type, "x-upsert": "true", "Cache-Control": "max-age=3600"}),
            data,
            timeout=300,
        )
        return f"{self.url}/storage/v1/object/public/{bucket}/{path}"
