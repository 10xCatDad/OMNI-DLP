# Research inventory

The baseline is yt-dlp commit
[`412e7f0a4b0588fe884a02b344ac66a06538475b`](https://github.com/yt-dlp/yt-dlp/tree/412e7f0a4b0588fe884a02b344ac66a06538475b).
This inventory describes declared upstream capabilities and reported issues.
It does not establish complete native rewrite parity or verify every website.

| Artifact | Contents |
| --- | --- |
| [Baseline and coverage](UPSTREAM.md) | Feature families, limitations, native versus delegated coverage, and implementation priorities |
| [Issue triage](ISSUES.md) | Snapshot coverage, label interpretation, and source-linked feature requests and defects |
| [Source summary](upstream-summary.json) | Revision, timestamps, checksums, completeness scope, and inventory counts |
| [CLI inventory](upstream-cli.json) | All 323 parser declarations and their 394 option spellings |
| [Extractor inventory](upstream-extractors.json) | All 1,754 registered handlers, 1,734 documented entries, broken markers, and explicitly unsupported URL patterns |
| [Pipeline inventory](upstream-pipeline.json) | Downloaders, protocol dispatch, external integrations, postprocessors, and network backends |
| [Open issue snapshot](upstream-issues.json) | Metadata for 2,074 open issues, pagination evidence, counts, and consistency caveats |

Generate source inventories with
[`scripts/inventory_upstream.py`](../../scripts/inventory_upstream.py) against a
trusted checkout. Refresh open issue metadata with
[`scripts/snapshot_issues.py`](../../scripts/snapshot_issues.py). Both scripts
record their scope; the issue collector records failures and incomplete runs.
See [UPSTREAM.md](UPSTREAM.md#refreshing-the-inventory) and
[ISSUES.md](ISSUES.md#refresh) for commands.

The source registry includes redirect/error handlers, so extractor counts are
not counts of distinct working websites. The issue traversal completed, but
GitHub repository metadata counted two more records than both traversal and
search. That discrepancy remains explicit in the snapshot.

[YouTube challenge research](YOUTUBE-CHALLENGES.md) records independently verified
signature-transform feasibility and the remaining compiler/runtime requirements.
