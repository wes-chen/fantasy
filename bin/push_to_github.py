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
DEFAULT_FILES = ["SKILL.md", "bin/trade_board.py", "bin/fantasy_insights.py",
                 "tests/test_golden.py", "references/api_notes.md",
                 "bin/push_to_github.py"]
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


def get_issue(url):
    """GET the issue; returns dict or None on failure."""
    try:
        return api(url)
    except RuntimeError as e:
        print(f"  issue lookup failed ({e})")
        return None


def comment_on_issue(number, body):
    try:
        api(f"https://api.github.com/repos/{REPO}/issues/{number}/comments",
            data=json.dumps({"body": body}).encode(), method="POST")
        print(f"  posted note to issue #{number}")
    except RuntimeError as e:
        print(f"  could not comment on issue #{number}: {e}")


def push_file(f, message, content, sha, retries=1):
    body = {"message": message, "content": content}
    if sha:
        body["sha"] = sha
    try:
        out = api(f"https://api.github.com/repos/{REPO}/contents/{f}",
                  data=json.dumps(body).encode(), method="PUT")
    except RuntimeError as e:
        if "409" not in str(e) or retries <= 0:
            raise
        # Stale blob SHA (concurrent push): re-fetch the fresh SHA and retry once.
        print(f"  {f}: 409 conflict (stale SHA) — re-fetching and retrying")
        cur = api(f"https://api.github.com/repos/{REPO}/contents/{f}")
        body["sha"] = cur.get("sha")
        out = api(f"https://api.github.com/repos/{REPO}/contents/{f}",
                  data=json.dumps(body).encode(), method="PUT")
    return out["commit"]["sha"][:8]


def remote_matches(f, content):
    """Re-fetch the remote file and byte-compare against local content."""
    try:
        cur = api(f"https://api.github.com/repos/{REPO}/contents/{f}")
    except RuntimeError as e:
        return False, f"re-fetch failed: {e}"
    remote_b64 = (cur.get("content") or "").replace("\n", "")
    if remote_b64 != content:
        return False, "remote content differs from local"
    return True, ""


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} \"<commit message>\" [file ...]",
              file=sys.stderr)
        sys.exit(2)
    message = sys.argv[1]
    # Mis-fire guard: a lone flag (e.g. --help) is never a commit message.
    # Without this, `push_to_github.py --help` pushed every file carrying
    # the garbage message "--help" (2026-10-07).
    if message.startswith("-"):
        print(f"Refusing to push: \"{message}\" looks like a flag, not a commit message.",
              file=sys.stderr)
        sys.exit(2)
    args = sys.argv[2:]
    issue_number = None
    files = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--issue":
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                issue_number = args[i + 1]
                i += 2
            else:
                i += 1
            continue
        if a.startswith("--issue="):
            issue_number = a.split("=", 1)[1]
            i += 1
            continue
        files.append(a)
        i += 1
    files = files or DEFAULT_FILES
    changed, skipped, failed, local_blobs = [], [], [], {}
    for f in files:
        local = os.path.join(SKILL_ROOT, f)
        if not os.path.isfile(local):
            print(f"  {f}: not found locally, skipping")
            skipped.append(f)
            continue
        with open(local, "rb") as fh:
            content = base64.b64encode(fh.read()).decode()
        local_blobs[f] = content
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
        try:
            short = push_file(f, message, content, sha, retries=1)
        except RuntimeError as e:
            print(f"  {f}: PUSH FAILED ({e})")
            failed.append(f)
            continue
        changed.append((f, short))
        print(f"  {f}: pushed ({short})")
    for f in skipped:
        print(f"  {f}: unchanged")
    # Post-push verification: re-fetch every intended file and byte-compare
    # remote vs local. If any file failed or mismatches, fail loudly —
    # a "Closes #N" commit message may already have auto-closed the issue,
    # so a silent partial push is worse than a loud one (#17).
    mismatches = list(failed)
    for f, content in local_blobs.items():
        if f in failed:
            continue
        ok, why = remote_matches(f, content)
        if not ok:
            print(f"  VERIFY FAILED {f}: {why}")
            mismatches.append(f)
    if mismatches:
        note = ("Automated push verification (#17) FAILED: these files did not "
                f"land on main as intended: {', '.join(mismatches)}. "
                "The commit carrying \"Closes\" may already have auto-closed the "
                "issue — reopening/flagging for review. The remaining files will "
                "push on the next run's unchanged-detection, detached from this "
                "issue, so treat this change set as split until manually resolved.")
        print(f"\nVERIFY FAILED: {len(mismatches)} file(s) not confirmed on remote: "
              f"{', '.join(mismatches)}")
        if issue_number:
            comment_on_issue(issue_number, note)
            issue = get_issue(f"https://api.github.com/repos/{REPO}/issues/{issue_number}")
            if issue and issue.get("state") == "closed":
                try:
                    api(f"https://api.github.com/repos/{REPO}/issues/{issue_number}",
                        data=json.dumps({"state": "open"}).encode(), method="PATCH")
                    print(f"  reopened issue #{issue_number}")
                except RuntimeError as e:
                    print(f"  could not reopen issue #{issue_number}: {e}")
        else:
            print("  (no --issue given, so no issue note was posted)")
        sys.exit(1)
    print(f"done: {len(changed)} pushed, {len(skipped)} unchanged, "
          f"all verified byte-identical")


if __name__ == "__main__":
    sys.exit(main())
