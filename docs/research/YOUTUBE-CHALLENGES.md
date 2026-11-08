# YouTube challenge research

Research date: 2026-11-07. Status: **experimental native signature compiler;
not connected to downloads, no live playback verification**.

`omni_dlp.youtube_challenges` now compiles recognized signature operations into
immutable, bounded transform plans. It automatically derives both plans below
and matches all three public oracle vectors. No downloaded JavaScript is
executed, no yt-dlp implementation is imported, and no runtime was installed.
A separate implementation is still needed for the more complex `n` challenge.

The compiler remains an experimental module outside the extractor's download
path. **A unique recognizable transform is not proof that the player uses it for
signatures.** Establishing that role at the actual signature call site is a
remaining integration requirement. Its subset assumes standard string/array
intrinsics. It rejects several explicit native-method mutations and dynamic
evaluation patterns, but does not prove arbitrary whole-program JavaScript
equivalence or model indirect global changes. Passing a prototype to an unrelated
mutating function is one example outside that analysis. These are documented
limitations, not claims that all incorrect or unsafe patterns are rejected.

## Implemented and tested this round

- Bounded tokenization and structural expression analysis, including literal
  string tables, same-source constant dispatcher arguments and XOR arithmetic.
- Complete recognition of reverse, prefix removal and modulo-swap helpers;
  typed immutable plans, source digests and UTF-16 application semantics.
- Rejection of ambiguous candidates, changed/escaped helpers, conditional or
  unrelated-scope declarations, detached methods, malformed syntax, unsupported
  dataflow, automatic-semicolon pitfalls and exhausted resource budgets.
- 142 independently authored offline tests. Review regressions cover helper
  return values, slice aliases, variable redeclarations, primitive arguments,
  all JavaScript line terminators and recursion limits.
- An explicit optional corpus verifier that accepts local assets or downloads
  the two hash-pinned public assets. It executes only OMNI-DLP's own compiler.

With the project installed, run:

```sh
python -m pytest tests/test_youtube_challenges.py
python scripts/verify_youtube_signatures.py --download
```

For an existing corpus, replace `--download` with `--player-directory DIR`.
The verifier checks asset hashes and all three expected signatures. It does not
test a watch response, resolve `n`, or request playable media bytes. Full player
sources and live signed URLs are not committed.

## Observed evidence

The public watch-page check documented in [the extractor notes](../sites/youtube.md)
returned HTTP 429. Two static player assets nevertheless returned HTTP 200 through
ordinary unauthenticated HTTPS requests. Their URLs come from
[upstream's pinned challenge tests](https://github.com/yt-dlp/yt-dlp/blob/412e7f0a4b0588fe884a02b344ac66a06538475b/test/test_jsc/test_ejs_integration.py),
used only as a public behavioral oracle. These are baseline fixtures, not evidence
that today's watch page selects either player.

| Player | Bytes | SHA-256 |
| --- | ---: | --- |
| [74edf1a3](https://www.youtube.com/s/player/74edf1a3/player_ias.vflset/en_US/base.js) | 2,699,840 | `ed9a4ea0646332a6b6c973ebe5a915c62c43db89bb2bb1a31eba07d906ef8ff7` |
| [901741ab](https://www.youtube.com/s/player/901741ab/player_ias.vflset/en_US/base.js) | 2,701,648 | `f8b24f2bbcfc45233e2e3cf197b5831179e7d93e904c46c485acc9c6bee83bb9` |

The first contains version marker `youtube.player.web_20260310_06_RC00`.
Both use string tables and numeric XOR dispatcher functions. Searching for the
historical literal `split("")` signature pattern finds unrelated utilities.
Recognizing old helper names alone would miss the observed representation.

Static inspection decoded literal tables as data, resolved helper-property
indices, and classified the helper bodies. Independent Python list operations
then reproduced all three published signature examples for these two assets:

| Player | Derived plan, in execution order | Oracle matches |
| --- | --- | --- |
| `74edf1a3` | reverse; swap(57); reverse | 110 → 110 characters; 51 → 51 characters |
| `901741ab` | swap(45); drop(2); reverse; drop(2); swap(57); swap(68); drop(3) | 110 → 103 characters |

Here `swap(k)` exchanges element zero with `k % current_length`; `drop(k)` removes
the first `k` elements. The plan is derived from each downloaded asset, not from
its expected output. Expected values were read with Python's AST/literal parser
from the pinned test source, without executing or importing that source.

Useful reproduction landmarks in these exact bytes:

- `74edf1a3`: table `Q`, dispatcher `JI`, helper object `o4`, dispatcher XOR key
  `1870`. Table entries identify split, empty separator, join and helper methods.
- `901741ab`: table `I`, dispatcher `sc`, helper object `g7`, XOR key `827`.
  Its three helpers implement swap, prefix splice and reverse.

These names and constants are evidence, not production identifiers to hardcode.
The compiler derives them from each source and contains no player-ID or
hash-to-plan shortcuts. Three matching examples establish corpus compatibility,
not a general JavaScript interpreter or active-player compatibility.

The candidate `n` branch in the first player is substantially richer: a large
heterogeneous array, regular-expression literals, nested function references,
mutable arrays and numeric dispatch. No `n` transform was independently verified.
Solving signatures alone therefore leaves meaningful playback gaps, including
`n`, proof-of-origin tokens, client eligibility and SABR transport.

## Architecture and remaining integration work

1. Fetch only a validated YouTube player URL obtained from the requested page;
   bound response size and redirects. Cache source and plans by complete source
   SHA-256, compiler version and challenge kind. Never key solely by player ID.
2. Tokenize JavaScript with string/comment/template/regex awareness and strict
   source, nesting and token budgets. Decode only literal string-table creation.
   Do not use `eval`, Python execution, or substring brace counting as a parser.
3. Find split/array-transform/join branches structurally. Resolve dispatcher
   constants from literal arguments at call sites in the same source, then
   evaluate the selected branch and its table-index arithmetic. Require valid
   input/output dataflow; reject multiple plausible plans.
4. Parse each referenced helper completely. Accept only exact AST shapes for
   reverse, prefix splice/slice and modulo-index swap. Reject extra statements,
   unknown calls, getters, global references, or mutation outside the array.
5. Emit immutable `reverse`, `drop(n)` and `swap(n)` operations. Cap operations,
   input size and integer magnitude. Apply JavaScript integer/XOR and UTF-16
   semantics explicitly, or reject inputs outside a documented supported subset.
   Handle empty arrays and changing lengths before modulo or indexed operations.
6. Return a typed unsupported/error result when evidence is insufficient. Preserve
   the existing omission diagnostics. Apply the result to the cipher's specified
   signature query key without disturbing other query parameters.

Implemented signatures, with unsupported input reported by exception:

```text
compile_signature(player_source) -> SignaturePlan
apply_signature(plan, signature) -> str
# Both may raise UnsupportedChallenge. There is no solve_n implementation.
```

Keep compilation separate from application. If an optimization caches a mapping
of input positions, include input length in that cache key: modulo swaps can
produce different mappings for different lengths. Do not promote the three
observed matches into universal test coverage.

## The `n` path and an optional runtime

First prototype a bounded AST interpreter against the actual `n` dependency
closure. This requires a deliberate JavaScript subset, not simply the signature
operations. Reject unsupported syntax and count every operation and recursion
step. A synthetic test is insufficient to call the actual `n` branch supported.

An optional Deno adapter is a fallback design, not an implemented capability.
Deno was absent during this investigation; installed Node/Bun were not used to
execute the assets. There is no runtime isolation proof from this environment.

Deno's initial static module graph can load files without the normal read gate;
literal dynamic imports also enter that graph. Its permission broker can override
CLI allow/deny flags. Consequently, start only a trusted, import-free runner and
send the untrusted payload as JSON data after startup. Build a small environment
allowlist, excluding `DENO_PERMISSION_BROKER_PATH` and inherited runtime options.
Freezing globals is not a security boundary. Use OS isolation as an additional
layer. [Deno security model](https://docs.deno.com/runtime/fundamentals/security/)

The proposed runner must explicitly deny read, write, network, environment,
subprocess, FFI, system information and imports, with `--no-prompt`. Verify these
flags against the selected pinned runtime and refuse unsupported configurations.
[Deno permission flags](https://docs.deno.com/runtime/reference/permissions/)

Disable configuration/lock discovery, npm resolution, remote modules and code
caching using the documented `run` options. Supply an isolated empty working
directory/cache, controlled environment and no caller-provided flags. Accept only
a validated dependency closure with imports/global access excluded. Do not
concatenate a downloaded player into the runner's initial source graph.
[Deno run options](https://docs.deno.com/runtime/reference/cli/run/)

The parent process must enforce wall-time, memory and bounded stdout/stderr,
terminate the entire child process group on timeout, and validate one small JSON
result. Do not expose browser credentials, signed format URLs or caller headers
to the runner. The runtime cannot need network access to transform a string.

## Implementation gates

- Add independently authored miniature string-table/dispatcher fixtures, including
  renamed variables, comments, quoted braces and misleading lookalike branches.
  Test full helper-body rejection, ambiguity, malformed input and resource limits.
- Keep player URLs, hashes and normalized plans as corpus metadata. Download
  full player sources only in an explicit optional network test; do not vendor
  megabytes of third-party code or live signed media URLs into normal fixtures.
- Reproduce the three signature vectors before integrating automatic extraction.
  Add broader independently calculated length/edge cases, including surrogate
  pairs if the solver accepts arbitrary Unicode.
- Before runtime execution, test denied file/environment/network/process/import
  attempts, broker-environment stripping, infinite loops, output floods and heap
  exhaustion using project-authored adversarial fixtures.
- Require an actual public watch response plus a successful bounded media-byte
  read before claiming live support. Until available, report fixture compatibility
  and the exact unavailable challenges separately.

Remaining work: establish actual signature-role linkage, address indirect
prototype/global semantics, validate player URL discovery and bounded fetching,
integrate a reviewed compiler into extraction, expand player-family coverage, and
implement `n` handling. Cache design and runtime isolation above are proposals,
not shipped features. Full native YouTube parity is not achieved by this compiler
alone.
