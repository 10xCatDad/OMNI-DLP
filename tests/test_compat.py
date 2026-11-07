from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from omni_dlp import compat
from omni_dlp.models import OmniError


class CompatibilityTests(unittest.TestCase):
    def test_missing_dependency_is_actionable(self):
        with patch.object(compat, "available", return_value=False), patch.object(
            compat.subprocess, "run"
        ) as process:
            with self.assertRaisesRegex(OmniError, r"omni-dlp\[compat\]"):
                compat.run(["--help"])
            process.assert_not_called()

    def test_available_handles_missing_or_uninitialized_module(self):
        with patch.object(importlib.util, "find_spec", return_value=None):
            self.assertFalse(compat.available())
        with patch.object(importlib.util, "find_spec", side_effect=ValueError("no spec")):
            self.assertFalse(compat.available())

    def test_process_arguments_are_not_reparsed(self):
        arguments = ["--help", "-o", "%(title)s ; $(not-a-command).%(ext)s", "--", "a b"]
        original = list(arguments)
        with patch.object(compat, "available", return_value=True), patch.object(
            compat.subprocess, "run", return_value=types.SimpleNamespace(returncode=9)
        ) as process:
            self.assertEqual(compat.run(arguments), 9)
        self.assertEqual(arguments, original)
        process.assert_called_once_with(
            [sys.executable, "-m", "yt_dlp", *original], check=False
        )

    def test_library_adapter_preserves_options_and_status(self):
        calls = []

        class Downloader:
            def __init__(self, options):
                calls.append(dict(options))
                options["private_change"] = True

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def download(self, urls):
                calls.append(list(urls))
                urls.clear()
                return 4

        options = {"format": "bv*+ba/b", "quiet": True}
        urls = ["https://example.test/watch?v=1"]
        module = types.SimpleNamespace(YoutubeDL=Downloader)
        with patch.object(compat, "available", return_value=True), patch.object(
            compat.importlib, "import_module", return_value=module
        ):
            self.assertEqual(compat.download_urls(urls, options), 4)
        self.assertEqual(calls, [options, urls])
        self.assertNotIn("private_change", options)
        self.assertEqual(len(urls), 1)

    def test_real_process_preserves_terminal_streams_environment_and_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = root / "yt_dlp"
            upstream.mkdir()
            (upstream / "__init__.py").write_text("", encoding="utf-8")
            (upstream / "__main__.py").write_text(
                "import json, os, sys\n"
                "print(json.dumps({'args':sys.argv[1:], 'stdin':sys.stdin.read(), "
                "'env':os.environ['OMNI_COMPAT_TEST'], 'cwd':os.getcwd()}))\n"
                "print('diagnostic', file=sys.stderr)\n"
                "raise SystemExit(7)\n",
                encoding="utf-8",
            )
            arguments = ["--unknown-future-option", "a b", "$(literal);&", "--", "-value"]
            env = dict(os.environ)
            source = str(Path(compat.__file__).resolve().parents[1])
            env["PYTHONPATH"] = os.pathsep.join([directory, source])
            env["OMNI_COMPAT_TEST"] = "inherited"
            result = subprocess.run(
                [sys.executable, "-c", "import sys; from omni_dlp.compat import run; "
                 "sys.exit(run(sys.argv[1:]))", *arguments],
                input="terminal input\n", text=True, capture_output=True,
                cwd=directory, env=env, timeout=20, check=False,
            )
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertEqual(result.stderr, "diagnostic\n")
            payload = json.loads(result.stdout)
            self.assertEqual(payload["args"], arguments)
            self.assertEqual(payload["stdin"], "terminal input\n")
            self.assertEqual(payload["env"], "inherited")
            self.assertEqual(Path(payload["cwd"]).resolve(), root.resolve())
