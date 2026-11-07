# Compatibility and the rewrite boundary

OMNI-DLP has an independently implemented, media-neutral native path and an
optional compatibility bridge. **The bridge runs yt-dlp; it is not a rewritten
implementation of yt-dlp.** Full native feature and site parity is not complete.

Install the extra in the same Python environment as OMNI-DLP:

```sh
python -m pip install '.[compat]'
```

From an installed package, use `python -m pip install 'omni-dlp[compat]'` once a
package release is available on the configured package index.

Invoke compatibility explicitly:

```sh
omni-dlp compat --help
omni-dlp compat -F 'https://example.org/watch/123'
omni-dlp compat -f 'bv*+ba/b' --write-subs 'https://example.org/watch/123'
```

Everything following `compat` is passed as a separate, unchanged argument to
`python -m yt_dlp` using OMNI-DLP's Python interpreter. Shell quoting follows the
user's shell, as with ordinary command-line programs. OMNI-DLP does not parse,
silently discard, or reinterpret those options. `compat --help` displays
upstream help. The subprocess inherits the terminal, environment, current
directory, and return code. Upstream configuration files and plugins also apply.

This makes the installed version's extractor catalogue, format selection,
playlists, authentication, subtitles, postprocessors, SponsorBlock integration,
and other upstream capabilities accessible while native implementations develop.
It does **not** establish that every site currently works, fix upstream bugs,
remove service restrictions, or add image-gallery support to upstream extractors.
External dependencies, including FFmpeg and JavaScript runtimes where required,
remain necessary. Refer to the installed version's help and the
[upstream documentation](https://github.com/yt-dlp/yt-dlp#readme) for its behavior.

Native options and additions are not automatically composed with compatibility
mode. In particular, native discovery, transfer rules, archive storage, metadata
records, and plugin interfaces are separate. A compatibility invocation uses
upstream's own download archive, output templates, metadata, and hooks.

The optional Python adapter exposes the same boundary:

```python
from omni_dlp.compat import available, download_urls

if available():
    status = download_urls(
        ["https://example.org/watch/123"],
        {"format": "best", "outtmpl": "%(title)s.%(ext)s"},
    )
```

`available()` only confirms that the module can be located. `download_urls()`
passes a copy of the options dictionary to `yt_dlp.YoutubeDL`, returns its download
status, and preserves upstream download exceptions. It does not flatten complex
playlists or multiple selected formats into native media records. Missing optional
installation raises `OmniError` with an installation hint.

Compatibility forwarding is tested offline, including arguments containing
spaces and shell metacharacters, inherited input/output, and nonzero exits.
Those tests prove the bridge contract, not the correctness of every upstream
option or extractor. Independent native parity requires a tracked capability,
an implementation without the bridge, and representative regression fixtures.
