#!/usr/bin/env python3
"""Publish Markdown files to Confluence Cloud with the Markdown Importer REST API.

Inputs are read from INPUT_* environment variables set by action.yml.
Standard library only, so the action needs no install step.
"""

import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DOCS_URL = "https://yamuno.com/docs/markdown-importer-for-confluence/rest-api"
TROUBLESHOOTING_URL = DOCS_URL + "/troubleshooting"

TITLE_SOURCES = ("frontmatter", "h1", "filename")
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
# Every later request would fail the same way, so stop at the first one.
FATAL_STATUSES = {
    401: "Unauthorized. The token is missing or invalid. Check the token secret.",
    402: "License expired. Markdown Importer for Confluence needs an active license when the token is created.",
    403: "Forbidden. The token has expired. Create a new token in the app's API tab and update the secret.",
    404: "Not found. Check space-id and parent-id, and that the token's creator can access them.",
}
# The API also answers 400 when Confluence rejects the page, for example
# because parent-id does not exist, so point at the IDs as well.
BAD_REQUEST_HINT = (
    "Check that the file is not empty, that parent-id is a page in the space, "
    "and that the token's creator can edit it."
)

FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?\r?\n)?(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", re.DOTALL)
FM_TITLE_RE = re.compile(r"^title[ \t]*:[ \t]*(.*?)[ \t]*$", re.MULTILINE)
FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
H1_RE = re.compile(r"^[ \t]{0,3}#[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")


def split_front_matter(text):
    """Return (front_matter, body). front_matter is "" when the file has none."""
    match = FRONT_MATTER_RE.match(text)
    if not match:
        return "", text
    return match.group(1) or "", text[match.end():]


def front_matter_title(front_matter):
    match = FM_TITLE_RE.search(front_matter)
    if not match:
        return ""
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip()


def first_h1(body):
    """First ATX H1 outside fenced code blocks."""
    fence = None
    for line in body.splitlines():
        fence_match = FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is None:
            h1 = H1_RE.match(line)
            if h1:
                return h1.group(1).strip()
    return ""


def filename_title(path):
    return re.sub(r"[-_]+", " ", Path(path).stem).strip()


def resolve_title(path, front_matter, body, title_from):
    """Pick the page title, falling back down frontmatter -> h1 -> filename."""
    order = TITLE_SOURCES[TITLE_SOURCES.index(title_from):]
    for source in order:
        if source == "frontmatter":
            title = front_matter_title(front_matter)
        elif source == "h1":
            title = first_h1(body)
        else:
            title = filename_title(path)
        if title:
            return title
    return filename_title(path)


def find_files(root, pattern):
    base = Path(root)
    if not base.is_dir():
        raise SystemExit(f"::error::path '{root}' is not a directory")
    return sorted(p for p in base.glob(pattern) if p.is_file())


def post(endpoint, token, payload):
    """POST one page. Returns (status, message). Retries 429 and 5xx."""
    data = json.dumps(payload).encode("utf-8")
    status, message = 0, ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = urllib.request.Request(
            endpoint,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "confluence-markdown-sync",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as err:
            status, raw = err.code, err.read()
        except urllib.error.URLError as err:
            status, raw = 0, str(err.reason).encode()
        except (http.client.HTTPException, OSError) as err:
            status, raw = 0, str(err).encode()
        message = parse_message(raw)
        if status not in RETRY_STATUSES and status != 0:
            return status, message
        if attempt < MAX_ATTEMPTS:
            wait = 2 ** attempt
            print(f"  got {status or 'network error'}, retrying in {wait}s")
            time.sleep(wait)
    return status, message


def parse_message(raw):
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        body = json.loads(text)
    except ValueError:
        return text[:300]
    if isinstance(body, dict) and "body" in body:
        return str(body["body"])[:300]
    return text[:300]


def write_summary(rows, dry_run):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    heading = "Confluence sync (dry run)" if dry_run else "Confluence sync"
    lines = [f"### {heading}", "", "| File | Page title | Result |", "| --- | --- | --- |"]
    for file, title, result in rows:
        safe_title = title.replace("|", "\\|")
        lines.append(f"| `{file}` | {safe_title} | {result} |")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def write_outputs(counts):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in counts.items():
            fh.write(f"{key}={value}\n")


def env(name, default=""):
    return os.environ.get(f"INPUT_{name}", default).strip()


def as_bool(value, name):
    lowered = value.lower()
    if lowered in ("true", "yes", "1"):
        return True
    if lowered in ("false", "no", "0", ""):
        return False
    raise SystemExit(f"::error::{name} must be true or false, got '{value}'")


def main():
    endpoint = env("ENDPOINT")
    token = env("TOKEN")
    space_id = env("SPACE_ID")
    parent_id = env("PARENT_ID")
    root = env("PATH") or "docs"
    pattern = env("GLOB") or "**/*.md"
    overwrite = as_bool(env("OVERWRITE", "true"), "overwrite")
    dry_run = as_bool(env("DRY_RUN", "false"), "dry-run")
    title_from = env("TITLE_FROM") or "frontmatter"
    delay = float(env("DELAY") or "1")

    if token:
        print(f"::add-mask::{token}")
    if title_from not in TITLE_SOURCES:
        raise SystemExit(f"::error::title-from must be one of {', '.join(TITLE_SOURCES)}")
    missing = [name for name, value in (
        ("space-id", space_id), ("parent-id", parent_id),
    ) if not value]
    if not dry_run:
        missing += [name for name, value in (("endpoint", endpoint), ("token", token)) if not value]
    if missing:
        raise SystemExit(f"::error::Missing required input(s): {', '.join(missing)}")
    if not space_id.isdigit():
        raise SystemExit(
            f"::error::space-id must be the numeric space ID, not the space key ('{space_id}'). "
            f"Find it in the app's Space selector. See {DOCS_URL}/api-reference#finding-space-and-page-ids"
        )
    if not parent_id.isdigit():
        raise SystemExit(f"::error::parent-id must be a numeric page ID, got '{parent_id}'")

    files = find_files(root, pattern)
    if not files:
        print(f"No files matched '{pattern}' under '{root}'.")
        write_outputs({"created": 0, "updated": 0, "skipped": 0, "failed": 0})
        return 0

    pages = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        front_matter, body = split_front_matter(text)
        pages.append((path.as_posix(), resolve_title(path, front_matter, body, title_from), body))

    # All pages go under one parent, so two files with the same title would
    # overwrite each other. Stop before sending anything.
    seen = {}
    for file, title, _ in pages:
        seen.setdefault(title.casefold(), []).append(file)
    clashes = [files_ for files_ in seen.values() if len(files_) > 1]
    if clashes:
        for files_ in clashes:
            print(f"::error::Same page title from several files: {', '.join(files_)}")
        raise SystemExit("Give each file a unique title (front matter title, H1 or filename).")

    rows = []
    counts = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
    fatal = None
    for index, (file, title, body) in enumerate(pages):
        if fatal:
            rows.append((file, title, "not sent"))
            continue
        if not body.strip():
            print(f"skip     {file} (empty)")
            rows.append((file, title, "skipped (empty)"))
            counts["skipped"] += 1
            continue
        if dry_run:
            print(f"dry-run  {file} -> \"{title}\"")
            rows.append((file, title, "dry run"))
            continue

        if index and delay > 0:
            time.sleep(delay)
        status, message = post(endpoint, token, {
            "spaceId": space_id,
            "parentId": parent_id,
            "pageTitle": title,
            "content": body,
            "overwrite": overwrite,
        })
        if status == 201:
            print(f"created  {file} -> \"{title}\"")
            rows.append((file, title, "created"))
            counts["created"] += 1
        elif status == 200:
            print(f"updated  {file} -> \"{title}\"")
            rows.append((file, title, "updated"))
            counts["updated"] += 1
        else:
            hint = f" {BAD_REQUEST_HINT}" if status == 400 else ""
            print(f"::error file={file}::{status or 'network error'} {message}{hint}")
            rows.append((file, title, f"failed ({status or 'network error'})"))
            counts["failed"] += 1
            if status in FATAL_STATUSES:
                fatal = status

    write_summary(rows, dry_run)
    write_outputs(counts)
    print(f"\ncreated {counts['created']}, updated {counts['updated']}, "
          f"skipped {counts['skipped']}, failed {counts['failed']}")

    if fatal:
        print(f"::error::{FATAL_STATUSES[fatal]} See {TROUBLESHOOTING_URL}")
        return 1
    if counts["failed"]:
        print(f"::error::{counts['failed']} file(s) failed. See {TROUBLESHOOTING_URL}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
