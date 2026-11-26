#!/usr/bin/env python3
"""Validate a pull request against the AppDB database layout and decide
whether it may be merged automatically.

The PR is read purely as git data (`git diff`, `git cat-file`): it is never
checked out or executed, so it is safe to run for pull requests from forks.

Two separate outcomes:
  * errors  - a submission that looks like data but is malformed. The check
              fails and the PR cannot merge until it is fixed.
  * reasons - the PR is valid but not eligible for auto-merge (it modifies
              existing files, touches other paths, comes from a new account,
              links to something odd, ...). The check passes; a maintainer
              merges it by hand.
Eligible = no errors and no reasons.

Exit status: 0 when there are no errors, 1 otherwise. The result is printed
as JSON for the workflow.
"""
from __future__ import annotations

import argparse
import datetime
import ipaddress
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

MAX_FILES = 12
MAX_TOTAL_BYTES = 1_000_000
MAX_SCREENSHOT_BYTES = 150 * 1024
MAX_SCREENSHOTS = 5
MAX_DESCRIPTION = 5000
WISHLIST_MAX_DESCRIPTION = 80
MAX_WISHLIST_NAME = 100
MIN_ACCOUNT_AGE_DAYS = 7
MAX_OPEN_PRS = 5

CPUS = {"x86-64", "i386", "arm64"}
STATUSES = {"platinum", "gold", "silver", "bronze", "garbage"}
SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "is.gd", "ow.ly", "rb.gy", "cutt.ly", "buff.ly", "shorturl.at", "tiny.cc"}

NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
# Wishlist directories are slugs; the backend loads nothing else.
WISHLIST_SLUG = r"[a-z0-9][a-z0-9-]*"
# Any wishlist entry index, whatever its directory name (to report bad names).
WISHLIST_ANY_INDEX = re.compile(r"data/wishlist/[^/]+/[^/]+/[^/]+/index\.json")
SW = r"data/software/(?P<s1>[^/]+)/(?P<s2>[^/]+)/(?P<name>%s)" % NAME
VER = r"/versions/(?P<ver>%s)" % NAME
PATTERNS = {
    "software": re.compile(SW + r"/index\.json"),
    "version": re.compile(SW + VER + r"/index\.json"),
    "report": re.compile(SW + VER + r"/reports/(?P<rid>[0-9a-f]{8})\.json"),
    "shot": re.compile(SW + VER + r"/reports/(?P<rid>[0-9a-f]{8})/(?P<file>screenshot-\d+\.(?:png|jpg))"),
    "wishlist": re.compile(r"data/wishlist/(?P<s1>[^/]+)/(?P<s2>[^/]+)/(?P<name>%s)/index\.json" % WISHLIST_SLUG),
}
SHOT_NAME = re.compile(r"screenshot-\d+\.(png|jpg)")
# Wishlist link labels are slugified short labels (same rule as the backend).
WISHLIST_LINK_LABEL = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
CATEGORY_ID = re.compile(r"[a-z0-9][a-z0-9-]*")

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


@dataclass
class Result:
    errors: list = field(default_factory=list)
    reasons: list = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def eligible(self) -> bool:
        return not self.errors and not self.reasons

    def markdown(self) -> str:
        def items(msgs):
            return "\n".join("- `%s`" % clean(m) for m in msgs)

        if self.errors:
            return "### AppDB validation failed\n\nThis pull request cannot be merged until these problems are fixed:\n\n%s\n" % items(self.errors)
        if self.reasons:
            return ("### Needs a maintainer\n\nThis pull request is valid but not eligible for automatic merging:\n\n%s\n\n"
                    "A maintainer will review it.\n" % items(self.reasons))
        return "### Validated\n\nAll checks passed; this pull request is being merged automatically.\n"

    def to_json(self) -> str:
        return json.dumps({"valid": self.valid, "eligible": self.eligible, "errors": self.errors,
                           "reasons": self.reasons, "markdown": self.markdown()})


def clean(text: str) -> str:
    """Make PR-controlled text safe to quote in a comment (no backticks/newlines, bounded)."""
    return str(text).replace("`", "'").replace("\n", " ").replace("\r", " ")[:300]


@dataclass
class Entry:
    status: str
    new_mode: str
    sha: str
    path: str


def git(repo: str, *args: str) -> bytes:
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True).stdout


def exists_in(repo: str, rev: str, rel: str) -> bool:
    return subprocess.run(["git", "-C", repo, "cat-file", "-e", "%s:%s" % (rev, rel)], capture_output=True).returncode == 0


def diff_entries(repo: str, merge_base: str, head: str) -> list:
    raw = git(repo, "diff", "--raw", "-z", "--no-renames", "--no-abbrev", merge_base, head).split(b"\0")
    entries = []
    for i in range(0, len(raw) - 1, 2):
        meta = raw[i].decode().lstrip(":").split()  # oldmode newmode oldsha newsha status
        entries.append(Entry(status=meta[4][0], new_mode=meta[1], sha=meta[3], path=raw[i + 1].decode("utf-8", "replace")))
    return entries


def blob(repo: str, sha: str) -> bytes:
    return git(repo, "cat-file", "blob", sha)


def blob_size(repo: str, sha: str) -> int:
    return int(git(repo, "cat-file", "-s", sha))


def parse_url(url) -> Optional[object]:
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return parts


def link_concern(url: str) -> Optional[str]:
    """Why a (valid) URL should not be auto-merged, or None if it looks fine."""
    parts = parse_url(url)
    if parts is None:
        return "is not an http(s) URL"
    if parts.scheme != "https":
        return "is not https"
    if "@" in parts.netloc:
        return "contains credentials"
    host = parts.hostname.lower()
    try:
        ipaddress.ip_address(host)
        return "points at a raw IP address"
    except ValueError:
        pass
    if host == "localhost" or "." not in host:
        return "does not look like a public host name"
    if host in SHORTENERS:
        return "is a URL shortener"
    return None


def expected_shard(name: str):
    return name[0], (name[:2] if len(name) > 1 else "_")


def load_json(repo, entry, errors):
    try:
        data = json.loads(blob(repo, entry.sha).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        errors.append("%s: not valid JSON" % entry.path)
        return None
    if not isinstance(data, dict):
        errors.append("%s: JSON must be an object" % entry.path)
        return None
    return data


def unknown_keys(entry, data, allowed, errors):
    extra = sorted(set(data) - set(allowed))
    if extra:
        errors.append("%s: unknown field(s): %s" % (entry.path, ", ".join(extra)))


def evaluate(repo: str, base: str, head: str, facts: dict, now=None) -> Result:
    """facts: merged_prs (int), created_at (aware datetime), open_prs (int)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    res = Result()

    try:
        mb = git(repo, "merge-base", base, head).decode().strip()
        entries = diff_entries(repo, mb, head)
    except subprocess.CalledProcessError as exc:
        res.errors.append("cannot compute the pull request's changes: %s" % clean(exc.stderr.decode(errors="replace")))
        return res

    if not entries:
        res.reasons.append("the pull request changes no files")
        return res
    if len(entries) > MAX_FILES:
        res.reasons.append("%d files changed (automatic merging is limited to %d)" % (len(entries), MAX_FILES))

    # Classify. Only new regular files in the data layout are submissions.
    items = []  # (kind, match, entry)
    total = 0
    for e in entries:
        if e.path.startswith("data/wishlist/") and e.path.rsplit("/", 1)[-1] == "votes.txt":
            res.errors.append("%s: votes are recorded by the wishlist app, not in pull requests" % e.path)
            continue
        if e.status != "A":
            res.reasons.append("%s: modifies or deletes an existing file" % e.path)
            continue
        if e.new_mode != "100644":
            res.errors.append("%s: must be a regular file (mode %s)" % (e.path, e.new_mode))
            continue
        total += blob_size(repo, e.sha)
        for kind, rx in PATTERNS.items():
            m = rx.fullmatch(e.path)
            if m:
                items.append((kind, m, e))
                break
        else:
            if WISHLIST_ANY_INDEX.fullmatch(e.path):
                res.errors.append("%s: wishlist directory name must be lowercase letters, digits and hyphens "
                                  "(data/wishlist/<s1>/<s2>/<slug>/index.json)" % e.path)
            else:
                res.reasons.append("%s: outside the software data layout" % e.path)
    if total > MAX_TOTAL_BYTES:
        res.reasons.append("the new files total %d bytes (automatic merging is limited to %d)" % (total, MAX_TOTAL_BYTES))

    base_names = {}
    for line in git(repo, "ls-tree", "-r", "--name-only", mb, "--", "data/software").decode().splitlines():
        m = re.fullmatch(r"data/software/[^/]+/[^/]+/([^/]+)/index\.json", line)
        if m:
            base_names[m.group(1).lower()] = m.group(1)

    pr_software = {m["name"] for k, m, _ in items if k == "software"}
    pr_versions = {(m["name"], m["ver"]) for k, m, _ in items if k == "version"}
    pr_shots = {(m["name"], m["ver"], m["rid"], m["file"]) for k, m, _ in items if k == "shot"}
    referenced = set()
    link_urls = []  # (path, url)
    has_link_items = False

    def sw_dir(m):
        return "data/software/%s/%s/%s" % (m["s1"], m["s2"], m["name"])

    for kind, m, e in items:
        if (m["s1"], m["s2"]) != expected_shard(m["name"]):
            s1, s2 = expected_shard(m["name"])
            root = "wishlist" if kind == "wishlist" else "software"
            res.errors.append("%s: wrong shard directory (expected data/%s/%s/%s/%s)" % (e.path, root, s1, s2, m["name"]))
            continue

        if kind == "shot":
            size = blob_size(repo, e.sha)
            if size > MAX_SCREENSHOT_BYTES:
                res.errors.append("%s: larger than 150 KB" % e.path)
            else:
                head_bytes = blob(repo, e.sha)[:8]
                ok = head_bytes.startswith(PNG_MAGIC) if m["file"].endswith(".png") else head_bytes.startswith(JPEG_MAGIC)
                if not ok:
                    res.errors.append("%s: not a valid %s image" % (e.path, "PNG" if m["file"].endswith(".png") else "JPEG"))
            continue

        data = load_json(repo, e, res.errors)
        if data is None:
            continue

        if kind == "software":
            has_link_items = True
            unknown_keys(e, data, {"name", "links", "cpu", "free", "categories"}, res.errors)
            if data.get("name") != m["name"]:
                res.errors.append("%s: name %r must match the directory name %r" % (e.path, data.get("name"), m["name"]))
            other = base_names.get(m["name"].lower())
            if other is not None:
                res.errors.append("%s: name differs only by case from the existing software %r" % (e.path, other))
            cats = data.get("categories")
            if not isinstance(cats, list) or not cats or not all(isinstance(c, str) for c in cats):
                res.errors.append("%s: categories must be a non-empty list" % e.path)
            else:
                for c in cats:
                    if not CATEGORY_ID.fullmatch(c) or not exists_in(repo, mb, "data/categories/%s.json" % c):
                        res.errors.append("%s: unknown category %r" % (e.path, c))
            cpu = data.get("cpu")
            if cpu is not None and (not isinstance(cpu, list) or not all(c in CPUS for c in cpu if isinstance(c, str)) or not all(isinstance(c, str) for c in cpu)):
                res.errors.append("%s: unknown cpu in %s" % (e.path, clean(cpu)))
            if data.get("free") is not None and not isinstance(data["free"], bool):
                res.errors.append("%s: free must be true or false" % e.path)
            links = data.get("links")
            if links is not None:
                if not isinstance(links, dict):
                    res.errors.append("%s: links must be an object" % e.path)
                else:
                    for k, url in links.items():
                        if parse_url(url) is None:
                            res.errors.append("%s: links.%s is not an http(s) URL" % (e.path, clean(k)))
                        else:
                            link_urls.append((e.path, url))

        elif kind == "wishlist":
            has_link_items = True
            unknown_keys(e, data, {"name", "description", "links"}, res.errors)
            name = data.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > MAX_WISHLIST_NAME:
                res.errors.append("%s: name is required (at most %d characters)" % (e.path, MAX_WISHLIST_NAME))
            desc = data.get("description")
            if desc is not None and (not isinstance(desc, str) or len(desc) > WISHLIST_MAX_DESCRIPTION):
                res.errors.append("%s: description must be text of at most %d characters" % (e.path, WISHLIST_MAX_DESCRIPTION))
            links = data.get("links")
            if not isinstance(links, dict) or not links:
                res.errors.append("%s: links is required and must be a non-empty object" % e.path)
            else:
                for k, url in links.items():
                    if not WISHLIST_LINK_LABEL.fullmatch(k):
                        res.errors.append("%s: invalid link label %s (lowercase letters, digits and hyphens, "
                                          "at most 32 characters)" % (e.path, clean(k)))
                    if parse_url(url) is None:
                        res.errors.append("%s: links.%s is not an http(s) URL" % (e.path, clean(k)))
                    else:
                        link_urls.append((e.path, url))

        elif kind == "version":
            has_link_items = True
            unknown_keys(e, data, {"version", "download", "issues"}, res.errors)
            if data.get("version") != m["ver"]:
                res.errors.append("%s: version %r must match the directory name %r" % (e.path, data.get("version"), m["ver"]))
            if parse_url(data.get("download")) is None:
                res.errors.append("%s: download is not an http(s) URL" % e.path)
            else:
                link_urls.append((e.path, data["download"]))
            issues = data.get("issues")
            if issues is not None and (not isinstance(issues, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in issues)):
                res.errors.append("%s: issues must be a list of numbers" % e.path)
            if m["name"] not in pr_software and not exists_in(repo, mb, sw_dir(m) + "/index.json"):
                res.errors.append("%s: software %r does not exist" % (e.path, m["name"]))

        elif kind == "report":
            unknown_keys(e, data, {"status", "description", "screenshots"}, res.errors)
            if data.get("status") not in STATUSES:
                res.errors.append("%s: status must be one of %s" % (e.path, ", ".join(sorted(STATUSES))))
            desc = data.get("description")
            if not isinstance(desc, str) or not desc.strip() or len(desc) > MAX_DESCRIPTION:
                res.errors.append("%s: description is required (at most %d characters)" % (e.path, MAX_DESCRIPTION))
            shots = data.get("screenshots")
            if shots is not None:
                if not isinstance(shots, list) or len(shots) > MAX_SCREENSHOTS or not all(isinstance(s, str) and SHOT_NAME.fullmatch(s) for s in shots):
                    res.errors.append("%s: screenshots must be a list of at most %d screenshot-N.png/jpg names" % (e.path, MAX_SCREENSHOTS))
                else:
                    for s in shots:
                        key = (m["name"], m["ver"], m["rid"], s)
                        referenced.add(key)
                        if key not in pr_shots:
                            res.errors.append("%s: screenshot %s is not part of this pull request" % (e.path, s))
            if (m["name"], m["ver"]) not in pr_versions and not exists_in(repo, mb, sw_dir(m) + "/versions/%s/index.json" % m["ver"]):
                res.errors.append("%s: version %r of %r does not exist" % (e.path, m["ver"], m["name"]))

    for kind, m, e in items:
        if kind == "shot" and (m["name"], m["ver"], m["rid"], m["file"]) not in referenced:
            res.errors.append("%s: screenshot is not referenced by a report in this pull request" % e.path)

    involved = {m["name"] for _, m, _ in items}
    if len(involved) > 1:
        res.reasons.append("the pull request touches more than one software")

    # Trust: software and versions carry links to the outside world.
    if has_link_items:
        if facts["merged_prs"] < 1:
            res.reasons.append("adding software or a version needs a maintainer's review: the author has no earlier merged pull requests")
        for path, url in link_urls:
            concern = link_concern(url)
            if concern:
                res.reasons.append("%s: link %s %s" % (path, clean(url), concern))

    age_days = (now - facts["created_at"]).days
    if age_days < MIN_ACCOUNT_AGE_DAYS:
        res.reasons.append("the author's account is younger than %d days" % MIN_ACCOUNT_AGE_DAYS)
    if facts["open_prs"] > MAX_OPEN_PRS:
        res.reasons.append("the author has %d open pull requests (limit %d)" % (facts["open_prs"], MAX_OPEN_PRS))

    return res


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--repo", default=".")
    p.add_argument("--base", required=True, help="base commit (the branch the PR targets)")
    p.add_argument("--head", required=True, help="PR head commit")
    p.add_argument("--merged-prs", type=int, required=True, help="author's earlier merged PRs in this repo")
    p.add_argument("--open-prs", type=int, required=True, help="author's open PRs in this repo (including this one)")
    p.add_argument("--author-created-at", required=True, help="ISO timestamp the author's account was created")
    args = p.parse_args(argv)

    created = datetime.datetime.fromisoformat(args.author_created_at.replace("Z", "+00:00"))
    result = evaluate(args.repo, args.base, args.head,
                      {"merged_prs": args.merged_prs, "open_prs": args.open_prs, "created_at": created})
    print(result.to_json())
    return 0 if result.valid else 1


if __name__ == "__main__":
    sys.exit(main())
