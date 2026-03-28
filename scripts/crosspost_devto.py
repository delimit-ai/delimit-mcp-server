#!/usr/bin/env python3
"""
Cross-post markdown articles to Dev.to.

Reads articles from a content directory, publishes or updates them
on Dev.to via the Forem API. Idempotent — tracks published article
IDs in a local manifest to support updates.

Usage:
    # Publish all articles (dry run):
    python scripts/crosspost_devto.py --dir /home/delimit/delimit-private/blog --dry-run

    # Publish all articles:
    DEV_TO_API_KEY=your_key python scripts/crosspost_devto.py --dir /home/delimit/delimit-private/blog

    # Publish a single article:
    DEV_TO_API_KEY=your_key python scripts/crosspost_devto.py --file /home/delimit/delimit-private/blog/01-catch-breaking-api-changes.md

    # List published articles:
    DEV_TO_API_KEY=your_key python scripts/crosspost_devto.py --list

Requires:
    DEV_TO_API_KEY environment variable (get from https://dev.to/settings/extensions)
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: 'requests' package required. Install with: pip install requests")
    sys.exit(1)

API_BASE = "https://dev.to/api"
MANIFEST_FILE = ".devto_manifest.json"


def get_api_key():
    key = os.environ.get("DEV_TO_API_KEY")
    if not key:
        print("ERROR: DEV_TO_API_KEY environment variable not set.")
        print("Get your key from: https://dev.to/settings/extensions")
        sys.exit(1)
    return key


def load_manifest(content_dir):
    """Load the manifest that maps filenames to Dev.to article IDs."""
    manifest_path = Path(content_dir) / MANIFEST_FILE
    if manifest_path.exists():
        with open(manifest_path) as f:
            return json.load(f)
    return {}


def save_manifest(content_dir, manifest):
    """Save the manifest after publishing."""
    manifest_path = Path(content_dir) / MANIFEST_FILE
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"  Manifest saved: {manifest_path}")


def parse_frontmatter(filepath):
    """Parse YAML-ish frontmatter from a markdown file.

    Returns (frontmatter_dict, body_markdown).
    """
    text = Path(filepath).read_text(encoding="utf-8")

    # Match frontmatter block
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
    if not match:
        print(f"  WARNING: No frontmatter found in {filepath}")
        return {}, text

    fm_text = match.group(1)
    body = match.group(2).strip()

    # Simple key: value parser (handles quoted and unquoted values)
    fm = {}
    for line in fm_text.strip().split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Parse boolean
        if value.lower() == "true":
            value = True
        elif value.lower() == "false":
            value = False
        fm[key] = value

    return fm, body


def build_article_payload(filepath, publish=False):
    """Build the Dev.to article creation/update payload."""
    fm, body = parse_frontmatter(filepath)

    if not fm.get("title"):
        print(f"  ERROR: Article {filepath} has no title in frontmatter.")
        return None

    # Parse tags from comma-separated string
    tags = []
    if fm.get("tags"):
        if isinstance(fm["tags"], str):
            tags = [t.strip() for t in fm["tags"].split(",")]
        else:
            tags = fm["tags"]
    # Dev.to allows max 4 tags
    tags = tags[:4]

    payload = {
        "article": {
            "title": fm["title"],
            "body_markdown": body,
            "published": publish,
            "tags": tags,
        }
    }

    if fm.get("description"):
        payload["article"]["description"] = fm["description"]
    if fm.get("canonical_url"):
        payload["article"]["canonical_url"] = fm["canonical_url"]
    if fm.get("cover_image"):
        payload["article"]["cover_image"] = fm["cover_image"]
    if fm.get("series"):
        payload["article"]["series"] = fm["series"]

    return payload


def create_article(api_key, payload):
    """Create a new article on Dev.to."""
    resp = requests.post(
        f"{API_BASE}/articles",
        headers={
            "api-key": api_key,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def update_article(api_key, article_id, payload):
    """Update an existing article on Dev.to."""
    resp = requests.put(
        f"{API_BASE}/articles/{article_id}",
        headers={
            "api-key": api_key,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def list_articles(api_key):
    """List the authenticated user's articles."""
    resp = requests.get(
        f"{API_BASE}/articles/me/all",
        headers={"api-key": api_key},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def publish_file(api_key, filepath, manifest, content_dir, dry_run=False, publish=False):
    """Publish or update a single article."""
    filename = Path(filepath).name
    print(f"\nProcessing: {filename}")

    payload = build_article_payload(filepath, publish=publish)
    if not payload:
        return False

    title = payload["article"]["title"]
    existing_id = manifest.get(filename, {}).get("id")

    if dry_run:
        action = "UPDATE" if existing_id else "CREATE"
        status = "published" if publish else "draft"
        print(f"  [DRY RUN] Would {action} ({status}): {title}")
        if existing_id:
            print(f"  [DRY RUN] Existing article ID: {existing_id}")
        print(f"  [DRY RUN] Tags: {payload['article'].get('tags', [])}")
        return True

    try:
        if existing_id:
            print(f"  Updating article {existing_id}: {title}")
            result = update_article(api_key, existing_id, payload)
        else:
            print(f"  Creating article: {title}")
            result = create_article(api_key, payload)

        article_id = result["id"]
        article_url = result.get("url", f"https://dev.to/delimit_ai/{result.get('slug', '')}")

        manifest[filename] = {
            "id": article_id,
            "url": article_url,
            "title": title,
        }
        save_manifest(content_dir, manifest)

        status = "PUBLISHED" if publish else "DRAFT"
        print(f"  {status}: {article_url}")
        return True

    except requests.exceptions.HTTPError as e:
        print(f"  ERROR: {e}")
        if e.response is not None:
            print(f"  Response: {e.response.text[:500]}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Cross-post articles to Dev.to")
    parser.add_argument("--dir", help="Directory containing markdown articles")
    parser.add_argument("--file", help="Single markdown file to publish")
    parser.add_argument("--list", action="store_true", help="List published articles")
    parser.add_argument("--dry-run", action="store_true", help="Preview without publishing")
    parser.add_argument("--publish", action="store_true",
                        help="Publish articles (default: save as draft)")
    args = parser.parse_args()

    if args.list:
        api_key = get_api_key()
        articles = list_articles(api_key)
        if not articles:
            print("No articles found.")
            return
        print(f"Found {len(articles)} articles:\n")
        for a in articles:
            status = "PUBLISHED" if a.get("published") else "DRAFT"
            print(f"  [{status}] {a['title']}")
            print(f"    URL: {a.get('url', 'N/A')}")
            print(f"    ID:  {a['id']}")
            print()
        return

    if not args.dir and not args.file:
        parser.print_help()
        print("\nError: specify --dir or --file")
        sys.exit(1)

    api_key = None
    if not args.dry_run:
        api_key = get_api_key()

    content_dir = args.dir or str(Path(args.file).parent)
    manifest = load_manifest(content_dir)

    files = []
    if args.file:
        files = [args.file]
    else:
        files = sorted(
            str(p) for p in Path(args.dir).glob("*.md")
            if not p.name.startswith(".")
            and p.name not in ("README.md", "DEVTO_SETUP.md")
            and not p.name.isupper()  # skip ALL-CAPS docs like DEVTO_SETUP.md
        )

    if not files:
        print(f"No markdown files found in {args.dir}")
        sys.exit(1)

    print(f"Found {len(files)} article(s) to process")
    if args.dry_run:
        print("MODE: dry run (no API calls)")
    elif args.publish:
        print("MODE: publish (articles will be live)")
    else:
        print("MODE: draft (articles saved as drafts on Dev.to)")

    success = 0
    for filepath in files:
        if publish_file(api_key, filepath, manifest, content_dir,
                        dry_run=args.dry_run, publish=args.publish):
            success += 1

    print(f"\nDone: {success}/{len(files)} articles processed successfully.")


if __name__ == "__main__":
    main()
