# -*- coding: utf-8 -*-
"""Push the current workspace state to GitHub as a new commit on main (v0.1.1 fix)."""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = "Lanliy-Okami"
REPO_NAME = "zcode-agent-mode"
CONFIG = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), ".zcode", "cli", "config.json")
API = "https://api.github.com"

INCLUDE_DIRS = ["plugins/agent-mode", "tests"]
INCLUDE_FILES = ["README.md", "LICENSE", ".gitignore", "marketplace.json"]
EXCLUDE_PARTS = {"__pycache__"}
EXCLUDE_SUFFIXES = {".pyc"}


def read_token() -> str:
    with io.open(CONFIG, "r", encoding="utf-8") as handle:
        cfg = json.load(handle)
    header = cfg["mcp"]["servers"]["Github"]["headers"]["Authorization"]
    assert header.startswith("Bearer ")
    return header[len("Bearer "):].strip()


TOKEN = read_token()


def api(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        API + path, data=data, method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "agent-mode-publisher",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        raise SystemExit(f"GitHub API {method} {path} failed: {error.code}\n{error.read().decode('utf-8', 'replace')[:800]}")


def collect_files() -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for relative_file in INCLUDE_FILES:
        entries.append((relative_file, os.path.join(ROOT, relative_file.replace("/", os.sep))))
    for directory in INCLUDE_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, directory.replace("/", os.sep))):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_PARTS]
            for filename in filenames:
                if os.path.splitext(filename)[1].lower() in EXCLUDE_SUFFIXES:
                    continue
                absolute = os.path.join(dirpath, filename)
                entries.append((os.path.relpath(absolute, ROOT).replace(os.sep, "/"), absolute))
    return sorted(entries)


def main() -> int:
    files = collect_files()
    tree_entries = []
    for git_path, absolute in files:
        with io.open(absolute, "rb") as handle:
            content = handle.read()
        blob = api("POST", f"/repos/{OWNER}/{REPO_NAME}/git/blobs", {
            "content": base64.b64encode(content).decode("ascii"), "encoding": "base64",
        })
        tree_entries.append({"path": git_path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        print(f"  blob ok: {git_path}")

    ref = api("GET", f"/repos/{OWNER}/{REPO_NAME}/git/ref/heads/main")
    parent_sha = ref["object"]["sha"]
    parent = api("GET", f"/repos/{OWNER}/{REPO_NAME}/git/commits/{parent_sha}")
    tree = api("POST", f"/repos/{OWNER}/{REPO_NAME}/git/trees", {"base_tree": parent["tree"]["sha"], "tree": tree_entries})
    commit = api("POST", f"/repos/{OWNER}/{REPO_NAME}/git/commits", {
        "message": "v0.2.0: project configured ZCode MCP servers into run_code bindings (DSH-style SDK over MCP) — minimal MCP client (stdio + Streamable HTTP), cursor-paginated tools/list, fail-closed exclusive proxied calls, discovery cache surfaced by the UserPromptSubmit hook",
        "tree": tree["sha"],
        "parents": [parent_sha],
    })
    api("PATCH", f"/repos/{OWNER}/{REPO_NAME}/git/refs/heads/main", {"sha": commit["sha"], "force": False})
    print(f"pushed {commit['sha'][:12]} to main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
