# -*- coding: utf-8 -*-
"""Publish the Agent Mode repo to GitHub using the REST API (no git required).

Credentials are read from ZCode's user config (the same token the user's GitHub
MCP server uses). The token is never printed. Flow:
  create repo (auto_init) -> rewrite README placeholders with the real URL ->
  blobs -> tree (base_tree) -> commit -> PATCH refs/heads/main -> set topics.
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_NAME = "zcode-agent-mode"
CONFIG = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), ".zcode", "cli", "config.json")
API = "https://api.github.com"

# Paths relative to ROOT to publish (forward slashes). Dotfiles included.
INCLUDE_DIRS = ["plugins/agent-mode", "tests"]
INCLUDE_FILES = ["README.md", "LICENSE", ".gitignore", "marketplace.json"]
EXCLUDE_PARTS = {"__pycache__"}
EXCLUDE_SUFFIXES = {".pyc"}


def read_token() -> str:
    with io.open(CONFIG, "r", encoding="utf-8") as handle:
        cfg = json.load(handle)
    header = cfg["mcp"]["servers"]["Github"]["headers"]["Authorization"]
    assert header.startswith("Bearer "), "unexpected auth header shape"
    return header[len("Bearer "):].strip()


TOKEN = read_token()


def api(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        API + path,
        data=data,
        method=method,
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
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"GitHub API {method} {path} failed: {error.code}\n{body[:800]}")


def collect_files() -> list[tuple[str, str]]:
    """Return [(git_path, absolute_path)] with '/' separators."""
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
                git_path = os.path.relpath(absolute, ROOT).replace(os.sep, "/")
                entries.append((git_path, absolute))
    return sorted(entries)


def main() -> int:
    # 1. Who am I; does the repo already exist?
    status, me = api("GET", "/user")
    login = me["login"]
    print(f"authenticated as: {login}")
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"{API}/repos/{login}/{REPO_NAME}",
            headers={"Authorization": f"Bearer {TOKEN}", "User-Agent": "agent-mode-publisher"},
        ), timeout=30)
        raise SystemExit(f"repo {login}/{REPO_NAME} already exists — aborting to avoid touching it")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise SystemExit(f"could not check repo existence: {error.code}")
    print(f"repo name {REPO_NAME} is available")

    # 2. Create the public repo with an auto-init commit (gives us a main ref).
    status, repo = api("POST", "/user/repos", {
        "name": REPO_NAME,
        "description": "ZCode plugin porting the DeepSeek Harness (DSH) agent presets: PTC mode (programmatic tool calling via run_code) and minimal mode.",
        "private": False,
        "has_issues": True,
        "auto_init": True,
    })
    default_branch = repo.get("default_branch") or "main"
    print(f"created repo: {repo['html_url']} (branch: {default_branch})")

    # 3. Rewrite README placeholders with the real URL before pushing.
    repo_url = f"https://github.com/{login}/{REPO_NAME}"
    readme_path = os.path.join(ROOT, "README.md")
    with io.open(readme_path, "r", encoding="utf-8") as handle:
        readme = handle.read()
    readme = readme.replace("https://github.com/<YOUR-USER>/zcode-agent-mode", repo_url)
    with io.open(readme_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(readme)
    print(f"README marketplace link set to {repo_url}")

    # 4. Blobs.
    files = collect_files()
    print(f"publishing {len(files)} files")
    tree_entries = []
    for git_path, absolute in files:
        with io.open(absolute, "rb") as handle:
            content = handle.read()
        status, blob = api("POST", f"/repos/{login}/{REPO_NAME}/git/blobs", {
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        })
        tree_entries.append({"path": git_path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        print(f"  blob ok: {git_path}")

    # 5. Tree on top of the auto-init commit, then commit, then move main.
    status, ref = api("GET", f"/repos/{login}/{REPO_NAME}/git/ref/heads/{default_branch}")
    parent_commit_sha = ref["object"]["sha"]
    status, parent_commit = api("GET", f"/repos/{login}/{REPO_NAME}/git/commits/{parent_commit_sha}")
    base_tree = parent_commit["tree"]["sha"]
    status, tree = api("POST", f"/repos/{login}/{REPO_NAME}/git/trees", {"base_tree": base_tree, "tree": tree_entries})
    status, commit = api("POST", f"/repos/{login}/{REPO_NAME}/git/commits", {
        "message": "Agent Mode v0.1.0: port the DeepSeek Harness agent presets (PTC & minimal) to ZCode",
        "tree": tree["sha"],
        "parents": [parent_commit_sha],
    })
    api("PATCH", f"/repos/{login}/{REPO_NAME}/git/refs/heads/{default_branch}", {"sha": commit["sha"], "force": False})
    print("history: initial commit -> v0.1.0 tree commit on main")

    # 6. Topics.
    api("PUT", f"/repos/{login}/{REPO_NAME}/topics", {
        "names": ["zcode", "deepseek", "agent", "ptc", "run-code", "mcp", "plugin"]
    })

    print()
    print(f"DONE — repository URL: {repo_url}")
    print(f"marketplace link for users: {repo_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
