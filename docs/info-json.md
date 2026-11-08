# Verified info JSON sidecars

`--write-info-json` writes credential-redacted metadata next to the media output,
including during `--skip-download`. A sidecar such as `song.mp3.info.json` has its
own `song.mp3.info.json.omni.json` ownership receipt and persistent
`song.mp3.info.json.omni.lock` advisory lock. It does not use a `.part` file.

The writer verifies the sidecar's bytes and logical media identity before
updating it. A verified sidecar can refresh its title, metadata, download status,
or skipped status without `--force-overwrites`. Repeating identical metadata
leaves the sidecar and receipt unchanged. Hand-edited files, files belonging to
other media, and files without a valid receipt require another output name or
`--force-overwrites`. This includes legacy info JSON files written before
ownership receipts were introduced, even when their contents happen to match.

Logical identity prefers `media.metadata.archive_id`, then
`media.metadata.resource_id`, then `media.source_url`, and finally `media.url`.
Archive identity takes precedence because a selected format can introduce a
resource identity that is absent during `--skip-download`. The sidecar location
already identifies its destination; changing or adding the result's `path` field
does not change media ownership. Without a stable ID, a changed source URL can
require explicit replacement. Deleted sidecars can be recreated when their
remaining valid receipt identifies the same media.

The receipt contains only a schema version, a fixed document-kind label, byte
count, content SHA-256, and identity SHA-256. It does not copy source URLs,
credentials, metadata values, or local paths. These hashes detect unintended
changes; they are not signatures or authentication against someone who can edit
both the sidecar and its receipt.

## Safety and filesystem behavior

Output, receipt, and lock files must be regular files. Symbolic links and
symbolic-link parent directories are refused even with `--force-overwrites`.
Info JSON filenames must leave room for the receipt suffix within a 255-byte
filename limit; shorten the output template if this limit is exceeded.

JSON uses sorted keys, UTF-8 text, two-space indentation, and a final newline.
Records are limited to 16 MiB, 64 nesting levels, and 250,000 values. Nonfinite
numbers, circular structures, invalid Unicode, and non-JSON values are rejected.
Existing receipts are read with a 4 KiB limit; existing metadata reads are also
bounded. Duplicate JSON object keys are rejected when verifying existing files.

Both new files are prepared and flushed in sibling temporary files before
publication. Unix uses a hard link to publish a new destination without replacing
an intervening file. Windows uses its non-replacing rename for new destinations.
Updates of verified files and explicit overwrites use atomic replacement. The
filesystem must support these operations; an unsupported operation fails instead
of silently using a non-atomic fallback. The lock uses the existing platform
specific advisory locking implementation, with no external lock service.

Publication of the metadata and receipt is two separate filesystem operations.
An interruption between them can leave a new sidecar with a missing or stale
receipt. The next attempt refuses to treat this as verified ownership; inspect
the file and use `--force-overwrites` to recover. Individual files are flushed,
but this is not a guarantee of durability across every filesystem or power loss.
Locks coordinate cooperating writers. They do not provide a security boundary
against another process actively racing filesystem changes.

## Python API

```python
from pathlib import Path
from omni_dlp.cli import public_record
from omni_dlp.infojson import write_info_json

write_info_json(
    Path(result.path + ".info.json"),
    public_record(result.to_dict()),
    overwrite=False,
)
```

The caller must provide a redacted public record. The writer preserves supplied
record fields and does not independently discover or remove arbitrary secrets.
The CLI applies its public-record redaction before calling this API.

Offline tests cover real filesystem updates and duplicate writes, legacy and
tampered files, identity changes, orphan receipts, malformed and oversized JSON,
symlinks, locks, metadata redaction, competing edits, and simulated disk and
receipt-publication failures. Platform-specific branches still require the
project's platform test matrix to verify behavior on each supported system.
