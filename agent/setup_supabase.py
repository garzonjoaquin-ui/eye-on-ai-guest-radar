"""One-time setup: create the Supabase project, schema, audio bucket and seed data.

Needs SUPABASE_ACCESS_TOKEN in guest-radar/.env. Safe to run again.
Writes SUPABASE_URL / SUPABASE_ANON_KEY / SUPABASE_SERVICE_KEY to .env and site/config.js.
"""

import csv
import json
import os
import re
import secrets
import sys
import time

from common import ROOT, Supabase, http, load_env, set_env_value

API = "https://api.supabase.com/v1"
PROJECT_NAME = "guest-radar"


def mgmt(method, path, body=None):
    tok = os.environ["SUPABASE_ACCESS_TOKEN"]
    return http(method, f"{API}{path}", {"Authorization": f"Bearer {tok}"}, body, timeout=180)


def ensure_project():
    ref = os.environ.get("SUPABASE_PROJECT_REF")
    if ref:  # reuse an existing project instead of creating "guest-radar"
        print(f"Using project {ref}")
        return ref
    for p in mgmt("GET", "/projects"):
        if p["name"] == PROJECT_NAME:
            print(f"Project exists: {p['id']} ({p['status']})")
            return p["id"]
    orgs = mgmt("GET", "/organizations")
    org_id = os.environ.get("SUPABASE_ORG_ID") or (orgs[0]["id"] if orgs else None)
    if not org_id:
        org_id = mgmt("POST", "/organizations", {"name": "The Podmaker"})["id"]
        set_env_value("SUPABASE_ORG_ID", org_id)
        print(f"Created organization {org_id}")
    db_pass = secrets.token_urlsafe(24)
    p = mgmt("POST", "/projects", {
        "name": PROJECT_NAME,
        "organization_id": org_id,
        "region": "us-east-1",
        "db_pass": db_pass,
    })
    set_env_value("SUPABASE_DB_PASSWORD", db_pass)
    print(f"Created project {p['id']}")
    return p["id"]


def wait_healthy(ref):
    for _ in range(60):
        st = mgmt("GET", f"/projects/{ref}")["status"]
        if st == "ACTIVE_HEALTHY":
            return
        print(f"  waiting for project ({st})…")
        time.sleep(10)
    sys.exit("Project did not become healthy in 10 minutes.")


def keys(ref):
    """Return {'anon', 'service_role'}; prefers legacy JWT keys, falls back to new sb_publishable/sb_secret keys."""
    legacy, modern = {}, {}
    for k in mgmt("GET", f"/projects/{ref}/api-keys?reveal=true"):
        name, typ, val = k.get("name"), k.get("type") or "legacy", k.get("api_key")
        if not val:
            continue
        if typ == "legacy" and name in ("anon", "service_role"):
            legacy[name] = val
        elif typ == "publishable":
            modern.setdefault("anon", val)
        elif typ == "secret":
            modern.setdefault("service_role", val)
    out = legacy if len(legacy) == 2 else modern
    if len(out) < 2:
        raise RuntimeError("Could not read the project's API keys")
    return out


def run_sql(ref, sql):
    for attempt in range(6):
        try:
            return mgmt("POST", f"/projects/{ref}/database/query", {"query": sql})
        except RuntimeError as e:
            if attempt == 5:
                raise
            print(f"  SQL retry ({e})")
            time.sleep(10)


def ensure_bucket(sb):
    try:
        http("POST", f"{sb.url}/storage/v1/bucket", sb._h(),
             {"id": "briefings", "name": "briefings", "public": True,
              "allowed_mime_types": ["audio/mpeg", "audio/wav"]})
        print("Created storage bucket 'briefings'")
    except RuntimeError as e:
        if "already exists" in str(e) or "409" in str(e) or "Duplicate" in str(e):
            print("Bucket 'briefings' exists")
        else:
            raise


def seed(sb):
    # Past guests from the YouTube episode export (title format "... | Guest, Company").
    rows = {}
    path = ROOT.parent / "agente-editor" / "datos" / "episodios_eye_on_ai.csv"
    seed_csv = ROOT / "agent" / "past_guests_seed.csv"
    for src in (path, seed_csv):
        if not src.exists():
            continue
        with open(src, newline="") as f:
            for r in csv.DictReader(f):
                if "titulo" in r:
                    title = r["titulo"]
                    if "|" not in title:
                        continue
                    guest = title.rsplit("|", 1)[1].strip()
                    name = re.split(r",| of ", guest)[0].strip()
                    rows[name] = {"name": name, "detail": f"{title} ({r.get('fecha', '')})", "source": "youtube_export"}
                else:
                    rows[r["name"]] = {"name": r["name"], "detail": r.get("detail"), "source": "seed"}
    if rows:
        sb.insert("past_guests", list(rows.values()), upsert=True)
        print(f"Seeded {len(rows)} past guests")

    # Pilot week so the hub is never empty.
    if not sb.select("runs", "select=id&limit=1"):
        pilot = json.loads((ROOT / "site" / "data" / "pilot.json").read_text())
        r = pilot["run"]
        run = sb.insert("runs", [{"week_of": r["week_of"], "title": r["title"], "summary": r["summary"],
                                   "briefing_script": r.get("briefing_script"), "audio_url": r.get("audio_url"),
                                   "audio_seconds": r.get("audio_seconds"), "model": "manual-pilot"}])[0]
        guests = []
        for i, g in enumerate(pilot["guests"]):
            g = {k: v for k, v in g.items() if k != "id"}
            g.update(run_id=run["id"], position=i)
            guests.append(g)
        sb.insert("guests", guests)
        print(f"Seeded pilot week with {len(guests)} guests")


def write_site_config(url, anon):
    (ROOT / "site" / "config.js").write_text(
        "// Public settings for the hub (the anon key is meant to be public; access is limited by row-level security).\n"
        f"window.RADAR = {json.dumps({'supabaseUrl': url, 'anonKey': anon}, indent=2)};\n"
    )


def main():
    load_env()
    if not os.environ.get("SUPABASE_ACCESS_TOKEN"):
        sys.exit("Missing SUPABASE_ACCESS_TOKEN in guest-radar/.env")
    ref = ensure_project()
    wait_healthy(ref)
    url = f"https://{ref}.supabase.co"
    k = keys(ref)
    set_env_value("SUPABASE_URL", url)
    set_env_value("SUPABASE_ANON_KEY", k["anon"])
    set_env_value("SUPABASE_SERVICE_KEY", k["service_role"])
    run_sql(ref, (ROOT / "supabase" / "schema.sql").read_text())
    print("Schema applied")
    sb = Supabase(url, k["service_role"])
    ensure_bucket(sb)
    seed(sb)
    write_site_config(url, k["anon"])
    print(f"Done. Hub config written. Project: {url}")


if __name__ == "__main__":
    main()
