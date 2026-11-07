#!/usr/bin/env python3
"""Inventory a trusted, local yt-dlp checkout without loading user configuration.

This imports upstream Python modules. Only use a checkout you trust. No network
requests, downloads, configuration writes, or upstream contributions are made.
The output is a factual compatibility baseline, not a claim of working sites.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib
import json
import optparse
from pathlib import Path
import subprocess
import sys


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def class_inventory(directory: Path, suffix: str) -> list[dict]:
    result = []
    for path in sorted(directory.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name.endswith(suffix):
                result.append({
                    "class": node.name,
                    "module": f"yt_dlp.{directory.name}.{path.stem}",
                    "bases": [ast.unparse(base) for base in node.bases],
                })
    return result


def generate(source: Path, output: Path) -> dict:
    source = source.resolve(strict=True)
    if not (source / "yt_dlp/options.py").is_file():
        raise ValueError("source must be a yt-dlp source checkout")
    # This script runs in a new interpreter; insert the explicit source first.
    sys.path.insert(0, str(source))
    from yt_dlp.options import create_parser
    from yt_dlp.extractor import gen_extractor_classes
    from yt_dlp.downloader import PROTOCOL_MAP
    from yt_dlp.downloader.external import list_external_downloaders

    postprocessors = importlib.import_module("yt_dlp.postprocessor")
    parser = create_parser()
    options = []
    for group in [parser, *parser.option_groups]:
        for option in group.option_list:
            options.append({
                "names": [*option._short_opts, *option._long_opts],
                "group": getattr(group, "title", "Hidden / deprecated root options"),
                "destination": option.dest,
                "action": option.action,
                "type": option.type,
                "nargs": option.nargs,
                "metavar": option.metavar,
                "choices": option.choices,
                "hidden": option.help == optparse.SUPPRESS_HELP,
                "upstream_behavior": "declared",
                "independent_parity": "not_evaluated",
            })
    options.sort(key=lambda item: item["names"][-1])

    extractors = []
    for extractor in gen_extractor_classes():
        extractors.append({
            "class": extractor.__name__,
            "module": extractor.__module__,
            "key": extractor.ie_key(),
            "name": extractor.IE_NAME,
            "upstream_marked_working": extractor.working(),
            "listed_by_upstream": extractor.IE_DESC is not False,
            "role": "unsupported_site_classifier" if extractor.__module__.endswith(".unsupported") else "extractor_or_redirect_handler",
            "availability": "declared_not_live_verified",
            "independent_port": False,
        })
    extractors.sort(key=lambda item: item["name"].lower())

    supported_sites = []
    for line in (source / "supportedsites.md").read_text(encoding="utf-8").splitlines():
        if line.startswith(" - **"):
            supported_sites.append({
                "name": line.split("**", 2)[1],
                "upstream_marked_broken": "Currently broken" in line,
            })

    downloaders = class_inventory(source / "yt_dlp/downloader", "FD")
    processor_classes = class_inventory(source / "yt_dlp/postprocessor", "PP")
    protocols = {
        protocol: f"{cls.__module__}.{cls.__name__}"
        for protocol, cls in sorted(PROTOCOL_MAP.items())
    }
    registered_processors = sorted(postprocessors._default_pps)
    stamp = datetime.now(timezone.utc).isoformat()
    provenance = {
        "repository": "https://github.com/yt-dlp/yt-dlp",
        "commit": git(source, "rev-parse", "HEAD"),
        "commit_time": git(source, "show", "-s", "--format=%cI", "HEAD"),
        "captured_at": stamp,
        "source_tree_clean": not bool(git(source, "status", "--porcelain", "--untracked-files=no")),
        "method": "Upstream parser/registry introspection and Python AST; no live site tests",
        "source_sha256": {
            filename: hashlib.sha256((source / filename).read_bytes()).hexdigest()
            for filename in ["README.md", "supportedsites.md", "yt_dlp/options.py"]
        },
        "scope": "All option declarations and registered extractor classes in this commit; all top-level downloader and postprocessor classes with FD/PP suffixes. Not every implicit behavior, metadata field, plugin, historical option, or possible URL.",
    }
    summary = {
        "option_declarations": len(options),
        "option_spellings": sum(len(item["names"]) for item in options),
        "hidden_option_declarations": sum(item["hidden"] for item in options),
        "option_groups": dict(Counter(item["group"] for item in options)),
        "registered_extractors": len(extractors),
        "extractors_marked_broken": sum(not item["upstream_marked_working"] for item in extractors),
        "hidden_extractor_handlers": sum(not item["listed_by_upstream"] for item in extractors),
        "documented_site_entries": len(supported_sites),
        "documented_site_entries_marked_broken": sum(item["upstream_marked_broken"] for item in supported_sites),
        "downloader_classes": len(downloaders),
        "protocol_dispatch_entries": len(protocols),
        "registered_postprocessor_classes": len(registered_processors),
    }
    payloads = {
        "upstream-cli.json": {"provenance": provenance, "options": options},
        "upstream-extractors.json": {
            "provenance": provenance,
            "extractors": extractors,
            "documented_sites": supported_sites,
            "explicitly_unsupported_url_patterns": [
                {"category": extractor.IE_NAME, "patterns": list(extractor.URLS)}
                for extractor in gen_extractor_classes()
                if extractor.__module__.endswith(".unsupported")
            ],
        },
        "upstream-pipeline.json": {
            "provenance": provenance,
            "downloaders": downloaders,
            "protocol_dispatch": protocols,
            "default_downloader": "yt_dlp.downloader.http.HttpFD",
            "external_downloaders": list_external_downloaders(),
            "postprocessor_classes": processor_classes,
            "registered_postprocessors": registered_processors,
            "network_backends": sorted(path.stem for path in (source / "yt_dlp/networking").glob("_*.py") if path.stem not in {"__init__", "_helper"}),
        },
        "upstream-summary.json": {"provenance": provenance, "counts": summary},
    }
    output.mkdir(parents=True, exist_ok=True)
    for filename, data in payloads.items():
        (output / filename).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="trusted upstream checkout")
    parser.add_argument("--output", type=Path, default=Path("docs/research"))
    args = parser.parse_args()
    print(json.dumps(generate(args.source, args.output), indent=2))


if __name__ == "__main__":
    main()
