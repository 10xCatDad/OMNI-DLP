"""Real filesystem ownership, refresh, and failure checks for info sidecars."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from omni_dlp import infojson
from omni_dlp.cli import public_record
from omni_dlp.infojson import write_info_json
from omni_dlp.models import OmniError
from omni_dlp.transfer import _destination_lock


def record(**metadata):
    return {"media": {"url": "https://cdn.example/song.mp3", "title": "Song ♫",
                      "source_url": "https://example.test/song", "metadata": metadata},
            "downloaded": False}


def receipt(path):
    return Path(str(path) + ".omni.json")


def test_new_json_is_deterministic_and_receipt_contains_only_hashes(tmp_path):
    path = tmp_path / "nested" / "song.info.json"
    value = record(archive_id="service:song")
    write_info_json(path, value)
    assert json.loads(path.read_bytes()) == value
    assert path.read_bytes() == (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    marker = json.loads(receipt(path).read_bytes())
    assert marker == {"version": 1, "kind": "omni-info-json", "size": path.stat().st_size,
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "identity_sha256": marker["identity_sha256"]}
    assert len(marker["identity_sha256"]) == 64
    assert "https://" not in receipt(path).read_text()
    assert "service:song" not in receipt(path).read_text()
    assert Path(str(path) + ".omni.lock").is_file()
    assert not list(path.parent.glob(".omni-info-*"))
    assert not Path(str(path) + ".part").exists()


@pytest.mark.parametrize("overwrite", [False, True])
def test_identical_owned_writes_do_not_mutate_data_or_receipt(tmp_path, overwrite):
    path = tmp_path / "song.info.json"
    value = record()
    write_info_json(path, value)
    for item in (path, receipt(path)):
        os.utime(item, ns=(1_000_000_000, 1_000_000_000))
    before = [(item.stat().st_ino, item.stat().st_mtime_ns, item.read_bytes()) for item in (path, receipt(path))]
    write_info_json(path, copy.deepcopy(value), overwrite=overwrite)
    after = [(item.stat().st_ino, item.stat().st_mtime_ns, item.read_bytes()) for item in (path, receipt(path))]
    assert after == before


def test_owned_metadata_refresh_handles_skip_download_and_new_signed_media_url(tmp_path):
    path = tmp_path / "song.info.json"
    value = record(archive_id="service:song")
    write_info_json(path, value)
    original_identity = json.loads(receipt(path).read_bytes())["identity_sha256"]
    value["downloaded"] = True
    value["path"] = "song.mp3"
    value["skipped"] = False
    value["media"]["title"] = "Updated title"
    value["media"]["url"] = "https://new-cdn.example/renewed.mp3"
    value["media"]["metadata"]["resource_id"] = "service:song|format:high"
    write_info_json(path, value)
    assert json.loads(path.read_bytes()) == value
    assert json.loads(receipt(path).read_bytes())["identity_sha256"] == original_identity
    value["skipped"] = True
    write_info_json(path, value)
    assert json.loads(path.read_bytes())["skipped"] is True


@pytest.mark.parametrize("identity", ["resource", "source", "url"])
def test_logical_identity_fallbacks_allow_metadata_refresh(tmp_path, identity):
    path = tmp_path / "song.info.json"
    value = record(resource_id="asset:unique") if identity == "resource" else record()
    if identity == "url":
        del value["media"]["source_url"]
    write_info_json(path, value)
    value["media"]["title"] = "New title"
    write_info_json(path, value)
    assert json.loads(path.read_bytes()) == value


def test_legacy_unowned_even_identical_file_needs_overwrite(tmp_path):
    path = tmp_path / "song.info.json"
    original = (json.dumps(record()) + "\n").encode()
    path.write_bytes(original)
    with pytest.raises(OmniError, match="without an ownership receipt"):
        write_info_json(path, record())
    assert path.read_bytes() == original
    assert not receipt(path).exists()
    write_info_json(path, record(), overwrite=True)
    assert receipt(path).exists()


def test_modified_owned_file_requires_overwrite(tmp_path):
    path = tmp_path / "song.info.json"
    write_info_json(path, record())
    edited = b'{"manual": "important"}\n'
    path.write_bytes(edited)
    marker = receipt(path).read_bytes()
    with pytest.raises(OmniError, match="integrity check"):
        write_info_json(path, record())
    assert path.read_bytes() == edited
    assert receipt(path).read_bytes() == marker
    write_info_json(path, record(), overwrite=True)
    assert json.loads(path.read_bytes()) == record()


def test_different_media_identity_needs_overwrite_even_owned(tmp_path):
    path = tmp_path / "song.info.json"
    write_info_json(path, record(archive_id="service:first"))
    before = path.read_bytes()
    with pytest.raises(OmniError, match="different media"):
        write_info_json(path, record(archive_id="service:second"))
    assert path.read_bytes() == before
    write_info_json(path, record(archive_id="service:second"), overwrite=True)
    assert json.loads(path.read_bytes())["media"]["metadata"]["archive_id"] == "service:second"


@pytest.mark.parametrize("bad_receipt", [b"[]", b"garbage", b'{"version":1,"version":1}',
                                       b'{"version":NaN}', b"{" * 2000, b"\xff"])
def test_malformed_receipts_are_bounded_and_require_overwrite(tmp_path, bad_receipt):
    path = tmp_path / "song.info.json"
    write_info_json(path, record())
    original = path.read_bytes()
    receipt(path).write_bytes(bad_receipt)
    with pytest.raises(OmniError, match="invalid"):
        write_info_json(path, record())
    assert path.read_bytes() == original
    assert receipt(path).read_bytes() == bad_receipt
    write_info_json(path, record(), overwrite=True)
    assert json.loads(receipt(path).read_bytes())["kind"] == "omni-info-json"


def test_orphan_receipt_only_allows_same_identity_recovery(tmp_path):
    path = tmp_path / "song.info.json"
    write_info_json(path, record(archive_id="service:first"))
    path.unlink()
    with pytest.raises(OmniError, match="different media"):
        write_info_json(path, record(archive_id="service:second"))
    assert not path.exists()
    write_info_json(path, record(archive_id="service:first"))
    assert path.is_file()


@pytest.mark.parametrize("suffix", ["", ".omni.json", ".omni.lock"])
@pytest.mark.parametrize("overwrite", [False, True])
def test_symlink_targets_are_rejected_even_with_overwrite(tmp_path, suffix, overwrite):
    target = tmp_path / "private.txt"
    target.write_bytes(b"keep")
    path = tmp_path / "song.info.json"
    link = Path(str(path) + suffix)
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("Creating symlinks requires filesystem support and permission")
    with pytest.raises(OmniError, match="symbolic-link"):
        write_info_json(path, record(), overwrite=overwrite)
    assert link.is_symlink()
    assert target.read_bytes() == b"keep"


def test_symlink_parent_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(actual, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("Creating symlinks requires filesystem support and permission")
    with pytest.raises(OmniError, match="symbolic-link"):
        write_info_json(link / "song.info.json", record())
    assert not list(actual.iterdir())


@pytest.mark.parametrize("suffix", ["", ".omni.json", ".omni.lock"])
def test_nonregular_output_and_sidecars_rejected(tmp_path, suffix):
    path = tmp_path / "song.info.json"
    Path(str(path) + suffix).mkdir()
    with pytest.raises(OmniError, match="regular file"):
        write_info_json(path, record(), overwrite=True)


def test_destination_lock_prevents_concurrent_writer(tmp_path):
    path = tmp_path / "song.info.json"
    with _destination_lock(path):
        with pytest.raises(OmniError, match="Another download"):
            write_info_json(path, record())
    assert not path.exists()


def test_caller_redaction_and_receipt_do_not_expose_url_credentials(tmp_path):
    path = tmp_path / "song.info.json"
    value = record()
    value["media"]["url"] = "https://user:pass@cdn.example/song.mp3?token=signed-secret"
    value["media"]["source_url"] = "https://example.test/song?key=private-key"
    value["media"]["headers"] = {"Authorization": "Bearer header-secret"}
    write_info_json(path, public_record(value))
    for text in (path.read_text(), receipt(path).read_text()):
        assert all(secret not in text for secret in ("user:pass", "signed-secret", "private-key", "header-secret"))
    assert "https://" not in receipt(path).read_text()


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), {1: "bad key"}, b"bytes", "\ud800"])
def test_invalid_input_rejected_before_filesystem_mutation(tmp_path, invalid):
    path = tmp_path / "new" / "song.info.json"
    value = record()
    value["invalid"] = invalid
    with pytest.raises(OmniError):
        write_info_json(path, value)
    assert not path.parent.exists()


def test_cyclic_deep_and_oversized_input_rejected(tmp_path, monkeypatch):
    path = tmp_path / "song.info.json"
    value = record()
    value["cycle"] = value
    with pytest.raises(OmniError, match="circular"):
        write_info_json(path, value)
    value = record()
    nested = value
    for _ in range(70):
        nested["nested"] = {}
        nested = nested["nested"]
    with pytest.raises(OmniError, match="nesting"):
        write_info_json(path, value)
    monkeypatch.setattr(infojson, "_MAX_BYTES", 1024)
    with pytest.raises(OmniError, match="size limit"):
        write_info_json(path, record(description="a" * 2048))
    assert not path.exists()
    assert not receipt(path).exists()


@pytest.mark.parametrize("suffix", ["", ".omni.json"])
def test_oversized_existing_file_needs_explicit_overwrite(tmp_path, suffix):
    path = tmp_path / "song.info.json"
    target = Path(str(path) + suffix)
    with target.open("wb") as stream:
        stream.truncate(infojson._MAX_BYTES + 1)
    with pytest.raises(OmniError, match="size limit"):
        write_info_json(path, record())
    assert target.stat().st_size == infojson._MAX_BYTES + 1
    write_info_json(path, record(), overwrite=True)
    assert json.loads(path.read_bytes()) == record()


def test_missing_media_identity_rejected_without_files(tmp_path):
    path = tmp_path / "song.info.json"
    with pytest.raises(OmniError, match="identity"):
        write_info_json(path, {"media": {"title": "No URL or stable ID"}})
    assert not path.exists()
    assert not Path(str(path) + ".omni.lock").exists()


def test_long_portable_filename_rejected_before_any_output(tmp_path):
    path = tmp_path / ("x" * 246)
    with pytest.raises(OmniError, match="filename is too long"):
        write_info_json(path, record())
    assert not list(tmp_path.iterdir())


def test_editor_change_while_temporaries_are_written_is_not_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "song.info.json"
    write_info_json(path, record())
    original = infojson._temporary

    def edit_during_write(destination, content):
        temporary = original(destination, content)
        path.write_bytes(b"edited independently")
        return temporary

    monkeypatch.setattr(infojson, "_temporary", edit_during_write)
    with pytest.raises(OmniError, match="changed during"):
        write_info_json(path, record(description="Updated"))
    assert path.read_bytes() == b"edited independently"
    assert not list(tmp_path.glob(".omni-info-*"))


def test_failed_temporary_sync_preserves_old_files_and_cleans_temporary(tmp_path, monkeypatch):
    path = tmp_path / "song.info.json"
    write_info_json(path, record())
    before = (path.read_bytes(), receipt(path).read_bytes())

    def fail_sync(fd):
        raise OSError("simulated unavailable disk")

    monkeypatch.setattr(infojson.os, "fsync", fail_sync)
    with pytest.raises(OmniError, match="Cannot safely publish"):
        write_info_json(path, record(description="Updated"))
    assert (path.read_bytes(), receipt(path).read_bytes()) == before
    assert not list(tmp_path.glob(".omni-info-*"))


def test_receipt_publication_failure_fails_closed_on_next_write(tmp_path, monkeypatch):
    path = tmp_path / "song.info.json"
    write_info_json(path, record())
    original = infojson._publish

    def fail_receipt(temporary, destination, replace):
        if destination == receipt(path):
            raise OSError("simulated publication failure")
        original(temporary, destination, replace)

    with monkeypatch.context() as local:
        local.setattr(infojson, "_publish", fail_receipt)
        with pytest.raises(OmniError, match="Cannot safely publish"):
            write_info_json(path, record(description="Updated"))
    assert not list(tmp_path.glob(".omni-info-*"))
    with pytest.raises(OmniError, match="integrity check"):
        write_info_json(path, record(description="Updated"))
    write_info_json(path, record(description="Updated"), overwrite=True)
    assert json.loads(path.read_bytes())["media"]["metadata"]["description"] == "Updated"
