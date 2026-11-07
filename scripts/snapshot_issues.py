#!/usr/bin/env python3
"""Snapshot public open issues without retaining bodies or account information.

Uses unauthenticated HTTPS by default. For authenticated rate limits, explicitly
pass --gh with an account-scoped GitHub CLI executable. Credentials never enter
the snapshot. GitHub's issues endpoint also returns pull requests; these are
counted for pagination verification and excluded from the saved issue inventory.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch(endpoint: str, gh: str | None) -> object:
    for attempt in range(4):
        try:
            if gh:
                result = subprocess.run(
                    [gh, "api", endpoint], capture_output=True, text=True, check=True,
                )
                return json.loads(result.stdout)
            request = urllib.request.Request(
                "https://api.github.com/" + endpoint,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "omni-dlp-research",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except (urllib.error.URLError, subprocess.CalledProcessError):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default="yt-dlp/yt-dlp")
    parser.add_argument("--gh", help="Explicit account-scoped GitHub CLI executable")
    parser.add_argument("--output", type=Path, default=Path("docs/research/upstream-issues.json"))
    args = parser.parse_args()
    started = utc_now()
    issues: dict[int, dict] = {}
    all_numbers: set[int] = set()
    pull_requests: set[int] = set()
    errors: list[dict] = []
    pages = 0
    rows = 0
    terminal_page = False
    before = after = None
    search_counts: dict[str, dict] = {}
    try:
        before = fetch(f"repos/{args.repository}", args.gh)["open_issues_count"]
        page = 1
        while True:
            batch = fetch(
                f"repos/{args.repository}/issues?state=open&sort=created&direction=asc&per_page=100&page={page}",
                args.gh,
            )
            if not isinstance(batch, list):
                raise ValueError("Expected GitHub issue list")
            pages += 1
            rows += len(batch)
            for issue in batch:
                number = issue["number"]
                all_numbers.add(number)
                if "pull_request" in issue:
                    pull_requests.add(number)
                    continue
                issues[number] = {
                    "number": number,
                    "title": issue["title"],
                    "url": issue["html_url"],
                    "labels": sorted(label["name"] for label in issue["labels"]),
                    "state": issue["state"],
                    "created_at": issue["created_at"],
                    "updated_at": issue["updated_at"],
                }
            print(f"page {page}: {len(batch)} rows, {len(issues)} issues", flush=True)
            if len(batch) < 100:
                terminal_page = True
                break
            page += 1
        after = fetch(f"repos/{args.repository}", args.gh)["open_issues_count"]
        for kind in ("issue", "pr"):
            query = urlencode({"q": f"repo:{args.repository} is:{kind} is:open", "per_page": 1})
            result = fetch(f"search/issues?{query}", args.gh)
            search_counts[kind] = {
                "total_count": result["total_count"],
                "incomplete_results": result["incomplete_results"],
            }
    except Exception as exc:
        # Do not persist stderr, request headers, credentials, or machine paths.
        errors.append({"page": pages + 1, "error_type": type(exc).__name__})
    labels = Counter(label for issue in issues.values() for label in issue["labels"])
    document = {
        "schema_version": 1,
        "source": f"https://github.com/{args.repository}/issues",
        "api": f"https://api.github.com/repos/{args.repository}/issues",
        "state_filter": "open",
        "started_at": started,
        "finished_at": utc_now(),
        "pagination": {
            "per_page": 100,
            "sort": "created",
            "direction": "asc",
            "pages_fetched": pages,
            "terminal_page_reached": terminal_page,
            "rows_fetched_including_pull_requests": rows,
            "unique_rows_including_pull_requests": len(all_numbers),
            "duplicate_rows": rows - len(all_numbers),
            "pull_requests_excluded": len(pull_requests),
        },
        "coverage": {
            "scope": "All open issues returned by a complete traversal; closed issues, discussions and issue bodies are out of scope.",
            "pagination_complete": terminal_page and not errors,
            "repo_open_issues_and_prs_before": before,
            "repo_open_issues_and_prs_after": after,
            "unique_rows_match_both_repo_counts": before == after == len(all_numbers),
            "search_cross_check": search_counts,
            "issue_count_matches_search": search_counts.get("issue", {}).get("total_count") == len(issues),
            "consistency": "GitHub does not provide an atomic snapshot. Issues may open or close during traversal; matching endpoint counts are a cross-check, not proof of an atomic snapshot.",
        },
        "issue_count": len(issues),
        "label_counts": dict(sorted(labels.items())),
        "errors": errors,
        "issues": sorted(issues.values(), key=lambda issue: issue["number"]),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Saved {len(issues)} issues; complete={document['coverage']['pagination_complete']}")
    return 0 if terminal_page and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
