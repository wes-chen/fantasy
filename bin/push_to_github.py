#!/usr/bin/env python3
"""Push changed skill files to the wes-chen/fantasy GitHub repo.

Uses the connected custom.github credential via the Contents API
(gh CLI is not authenticated on this VM). Only uploads files whose
content actually changed.

Usage:
  push_to_github.py "<commit message>" [file ...]
  (defaults to the synced set: SKILL.md, bin/trade_board.py, tests/test_golden.py,
   references/api_notes.md, bin/push_to_github.py)
"""
import sys
import os
import json
import base64
import urllib.request
import urllib.error

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import add_surrogate_to_request, read_json_response

REPO = "wes-chen/fantasy"
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
DEFAULT_FILES = ["SKILL.md", "bin/trade_board.py", "tests/test_golden.py",
                 "references/api_notes.md", "bin/push_to_github.py"]
ALLOWED_HOSTS = ["api.github.com"]


def api(url, data=None, method="GET"):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "muse-agent")
    req.add_header("Content-Type", "application/json")
    add_surrogate_to_request(req, "custom.github",
                             allowed_hosts=ALLOWED_HOSTS)
    try:
        with urllib.request.urlopen(req) as resp:
            return read_json_response(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"GitHub API {e.code}: {body}")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} \"<commit message>\" [file ...]",
              file=sys.stderr)
        sys.exit(2)
    message = sys.argv[1]
    files = sys.argv[2:] or DEFAULT_FILES
    changed, skipped = [], []
    for f in files:
        local = os.path.join(SKILL_ROOT, f)
        if not os.path.isfile(local):
            print(f"  {f}: not found locally, skipping")
            skipped.append(f)
            continue
        with open(local, "rb") as fh:
            content = base64.b64encode(fh.read()).decode()
        try:
            cur = api(f"https://api.github.com/repos/{REPO}/contents/{f}")
            sha = cur.get("sha")
            remote_b64 = (cur.get("content") or "").replace("\n", "")
        except RuntimeError as e:
            if "404" not in str(e):
                print(f"  {f}: fetch failed ({e}), skipping")
                skipped.append(f)
                continue
            sha, remote_b64 = None, None  # new file: create without sha
        if remote_b64 == content:
            skipped.append(f)
            continue
        body = {"message": message, "content": content}
        if sha:
            body["sha"] = sha
        out = api(f"https://api.github.com/repos/{REPO}/contents/{f}",
                  data=json.dumps(body).encode(), method="PUT")
        changed.append((f, out["commit"]["sha"][:8]))
        print(f"  {f}: pushed ({out['commit']['sha'][:8]})")
    for f in skipped:
        print(f"  {f}: unchanged")
    print(f"done: {len(changed)} pushed, {len(skipped)} unchanged")


if __name__ == "__main__":
    sys.exit(main())
