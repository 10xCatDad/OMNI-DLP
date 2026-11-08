#!/usr/bin/env python3
"""EXPERIMENTAL RESEARCH: optional corpus verification, not production deciphering.

Never executes downloaded JavaScript. The compiler is disconnected from downloads;
successful examples do not establish signature-role linkage for unknown players.

Use --player-directory with already captured files, or --download to fetch the
two public pinned assets into a temporary directory. This is not a normal test
suite network dependency and is not a live playback check.

Behavioral vectors originate in the public yt-dlp test corpus at commit
412e7f0a4b0588fe884a02b344ac66a06538475b:
https://github.com/yt-dlp/yt-dlp/blob/412e7f0a4b0588fe884a02b344ac66a06538475b/test/test_jsc/test_ejs_integration.py
Only public test data is reproduced here, not its solver or runtime code.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import hashlib
from pathlib import Path
import tempfile
from urllib.request import Request, urlopen

from omni_dlp.youtube_challenges import MAX_SOURCE, apply_signature, compile_signature

_SIGNATURE = ('NJAJEij0EwRgIhAI0KExTgjfPk-MPM9MAdzyyPRt=BM8-XO5tm5hlMCSVpAiEA'
              'v7eP3CURqZNSPow8BXXAoazVoXgeMP7gH9BdylHCwgw=gwzz')
_CORPUS = {
    '74edf1a3': {
        'sha256': 'ed9a4ea0646332a6b6c973ebe5a915c62c43db89bb2bb1a31eba07d906ef8ff7',
        'vectors': [
            (_SIGNATURE,
             'NJAJEij0EwRgIhAI0KExTgjfPk-MPM9MAdzyyPRt=BM8-XO5tm5hzMCSVpAiEA'
             'v7eP3CURqZNSPow8BXXAoazVoXgeMP7gH9BdylHCwgw=gwzl'),
            (bytes.fromhex('000102250304050607080910111213141516171819202122232425262728293031323334353637383940414243444546474849').decode('ascii'),
             bytes.fromhex('000102250304050607080910111213141516171819202122232425262728293031323334353637383940414249444546474843').decode('ascii')),
        ],
    },
    '901741ab': {
        'sha256': 'f8b24f2bbcfc45233e2e3cf197b5831179e7d93e904c46c485acc9c6bee83bb9',
        'vectors': [
            (_SIGNATURE,
             'wgwCHlydB9Hg7PMegXoVzaoAXXB8woPSNZqRUC3Pe7vAEiApVSCMlhwmt5ON-8MB='
             '5RPyyzdAM9MPM-kPfjgTxEK0IAhIgRwE0jiEJA'),
        ],
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--player-directory', type=Path, help='Directory containing <player-id>.js files')
    source.add_argument('--download', action='store_true', help='Explicitly fetch the pinned public corpus')
    args = parser.parse_args()
    context = tempfile.TemporaryDirectory(prefix='omni-player-corpus-') if args.download else nullcontext(args.player_directory)
    matched = 0
    with context as directory:
        directory = Path(directory)
        for player, case in _CORPUS.items():
            path = directory / f'{player}.js'
            if args.download:
                url = f'https://www.youtube.com/s/player/{player}/player_ias.vflset/en_US/base.js'
                request = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'})
                with urlopen(request, timeout=30) as response:
                    if response.geturl() != url:
                        raise SystemExit('Unexpected player redirect')
                    data = response.read(MAX_SOURCE + 1)
                if len(data) > MAX_SOURCE:
                    raise SystemExit('Player response size limit exceeded')
            else:
                with path.open('rb') as stream:
                    data = stream.read(MAX_SOURCE + 1)
            if len(data) > MAX_SOURCE or hashlib.sha256(data).hexdigest() != case['sha256']:
                raise SystemExit(f'{player}: corpus digest mismatch; do not silently replace this fixture')
            plan = compile_signature(data.decode('utf-8'))
            for value, expected in case['vectors']:
                if apply_signature(plan, value) != expected:
                    raise SystemExit(f'{player}: signature oracle mismatch')
                matched += 1
            print(f'{player}: {len(case["vectors"])} signature vectors passed; '
                  + ', '.join(f'{op.kind}({op.argument})' for op in plan.operations))
    print(f'{matched} public vectors passed; JavaScript execution and live playback were not attempted.')


if __name__ == '__main__':
    main()
