"""One-time setup: create the GitHub repo, store the agent's secrets, turn on Pages and push the code.

Needs GITHUB_TOKEN (classic, scopes: repo + workflow) in guest-radar/.env, plus the Supabase
values written by setup_supabase.py. Safe to run again (it pushes the latest code).
"""

import base64
import os
import subprocess
import sys

from common import ROOT, http, load_env, set_env_value

REPO = os.environ.get("RADAR_REPO", "eye-on-ai-guest-radar")
API = "https://api.github.com"
SECRETS = ["ANTHROPIC_API_KEY", "GEMINI_API_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_KEY"]


def gh(method, path, body=None):
    return http(method, f"{API}{path}", {
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }, body)


def ensure_repo(owner):
    try:
        gh("GET", f"/repos/{owner}/{REPO}")
        print(f"Repo exists: {owner}/{REPO}")
    except RuntimeError as e:
        if "404" not in str(e):
            raise
        gh("POST", "/user/repos", {
            "name": REPO,
            "description": "Guest Radar for Eye on AI — weekly AI guest suggestions (by The Podmaker)",
            "private": False,  # GitHub Pages on a free account needs a public repo; no secrets live in the code.
            "has_wiki": False,
            "has_projects": False,
        })
        print(f"Created repo {owner}/{REPO}")


def set_secrets(owner):
    from nacl import encoding, public

    key = gh("GET", f"/repos/{owner}/{REPO}/actions/secrets/public-key")
    pk = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    box = public.SealedBox(pk)
    for name in SECRETS:
        val = os.environ.get(name)
        if not val:
            print(f"  (skipping secret {name}: not set yet)")
            continue
        enc = base64.b64encode(box.encrypt(val.encode())).decode()
        gh("PUT", f"/repos/{owner}/{REPO}/actions/secrets/{name}", {"encrypted_value": enc, "key_id": key["key_id"]})
        print(f"  secret {name} set")


def enable_pages(owner):
    try:
        gh("POST", f"/repos/{owner}/{REPO}/pages", {"build_type": "workflow"})
        print("GitHub Pages enabled")
    except RuntimeError as e:
        if "409" in str(e) or "already" in str(e).lower():
            gh("PUT", f"/repos/{owner}/{REPO}/pages", {"build_type": "workflow"})
            print("GitHub Pages already enabled")
        else:
            raise


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, check=check, capture_output=True, text=True)


def push(owner):
    if not (ROOT / ".git").exists():
        git("init", "-b", "main")
    git("config", "user.name", "The Podmaker")
    git("config", "user.email", f"{owner}@users.noreply.github.com")
    git("add", "-A")
    if git("diff", "--cached", "--quiet", check=False).returncode != 0:
        git("commit", "-m", "Update Guest Radar\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
    remote = f"https://github.com/{owner}/{REPO}.git"
    if git("remote", "get-url", "origin", check=False).returncode != 0:
        git("remote", "add", "origin", remote)
    auth = base64.b64encode(f"x-access-token:{os.environ['GITHUB_TOKEN']}".encode()).decode()
    r = git("-c", f"http.extraheader=AUTHORIZATION: basic {auth}", "push", "-u", "origin", "main", check=False)
    if r.returncode != 0:
        sys.exit("git push failed:\n" + r.stderr.replace(auth, "***"))
    print("Code pushed")


def main():
    load_env()
    if not os.environ.get("GITHUB_TOKEN"):
        sys.exit("Missing GITHUB_TOKEN in guest-radar/.env")
    owner = gh("GET", "/user")["login"]
    if os.environ.get("GITHUB_USER") != owner:
        set_env_value("GITHUB_USER", owner)
    ensure_repo(owner)
    push(owner)
    set_secrets(owner)
    enable_pages(owner)
    gh("POST", f"/repos/{owner}/{REPO}/actions/workflows/pages.yml/dispatches", {"ref": "main"})
    url = f"https://{owner}.github.io/{REPO}/"
    set_env_value("RADAR_HUB_URL", url)
    print(f"Hub will be live at {url} in a minute or two.")


if __name__ == "__main__":
    main()
