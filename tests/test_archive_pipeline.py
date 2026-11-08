"""Archive recovery and sidecar safety across native download operations."""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from omni_dlp.assets import AssetOptions
from omni_dlp.engine import Downloader, Options
from omni_dlp.models import Media, OmniError
from omni_dlp.transfer import TransferOptions


@pytest.fixture
def archive_web():
    requests = []
    payloads = {"/a": b"first public fixture", "/b": b"second public fixture",
                "/placeholder": b"<html>public post metadata only</html>"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            payload = payloads.get(self.path.split("?", 1)[0])
            self.send_response(200 if payload is not None else 404)
            self.send_header("Content-Length", str(len(payload or b"")))
            self.end_headers()
            self.wfile.write(payload or b"")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, payloads
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def downloader(directory, archive=None, **kwargs):
    transfer = TransferOptions(directory=str(directory), output="%(id)s.%(ext)s")
    return Downloader(Options(transfer=transfer, archive=str(archive) if archive else None, **kwargs))


def media(base, identifier="a", extension="mp4", *, description=None):
    metadata = {"archive_id": "fixture:" + identifier}
    if description is not None:
        metadata["description"] = description
    return Media(base + "/" + identifier, title=identifier, id=identifier,
                 kind="video", ext=extension, metadata=metadata)


@pytest.mark.parametrize("method", ["fetch_one", "fetch"])
def test_metadata_only_placeholder_cannot_download_webpage(archive_web, tmp_path, method):
    base, requests, _ = archive_web
    client = downloader(tmp_path)
    item = Media(base + "/placeholder", id="post", kind="other",
                 metadata={"formats_required": True, "metadata_only": True})
    argument = item if method == "fetch_one" else [item]
    with pytest.raises(OmniError, match="No playable"):
        getattr(client, method)(argument)
    assert requests == []
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("use_archive", [False, True])
@pytest.mark.parametrize("kind,extension", [("image", "png"), ("document", "pdf")])
def test_native_asset_identity_survives_query_refresh(archive_web, tmp_path, use_archive, kind, extension):
    base, requests, payloads = archive_web
    archive = tmp_path / "archive.sqlite" if use_archive else None
    client = downloader(tmp_path, archive)
    original = replace(media(base, extension=extension), kind=kind, url=base + "/a?signature=first")
    first = client.fetch_one(original)
    refreshed = replace(original, url=base + "/a?signature=renewed")
    second = client.fetch_one(refreshed)
    assert second.skipped and second.sha256 == first.sha256
    assert Path(second.path).read_bytes() == payloads["/a"]
    assert requests == ["/a?signature=first"]
    assert second.media.metadata["resource_id"] == "fixture:a"
    assert "resource_id" not in original.metadata
    assert "signature" not in Path(first.path + ".omni.json").read_text()


def test_explicit_resource_identity_is_preserved(archive_web, tmp_path):
    base, requests, _ = archive_web
    client = downloader(tmp_path, tmp_path / "archive.sqlite")
    original = media(base)
    original.metadata["resource_id"] = "specific:representation"
    client.fetch_one(original)
    changed = replace(original, url=base + "/a?signature=renewed",
                      metadata={**original.metadata, "archive_id": "different:logical-id"})
    result = client.fetch_one(changed)
    assert result.skipped and result.media.metadata["resource_id"] == "specific:representation"
    assert len(requests) == 1


def test_archive_hit_recovers_assets_beside_prior_directory_file(archive_web, tmp_path):
    base, requests, payloads = archive_web
    archive = tmp_path / "archive.sqlite"
    original = media(base, description="Public description")
    first = downloader(tmp_path / "old", archive).fetch_one(original)
    current = downloader(tmp_path / "new", archive, assets=AssetOptions(write_description=True))
    result = current.fetch([original])[0]
    assert result.skipped and result.path == first.path
    assert len(result.attachments) == 1
    description = Path(result.attachments[0].path)
    assert description == tmp_path / "old" / "a.description"
    assert description.read_text() == "Public description"
    assert Path(first.path).read_bytes() == payloads["/a"]
    assert requests == ["/a"]
    assert current.options.transfer.directory == str(tmp_path / "new")
    assert not (tmp_path / "new" / "a.description").exists()
    repeated = current.fetch([original])[0]
    assert repeated.skipped and repeated.attachments[0].skipped
    assert requests == ["/a"]


@pytest.mark.parametrize("jobs", [1, 2])
def test_archived_sidecar_collisions_fail_before_any_write(archive_web, tmp_path, jobs):
    base, requests, payloads = archive_web
    archive = tmp_path / "archive.sqlite"
    items = [media(base, "a", "mp4", description="Description A"),
             media(base, "b", "webm", description="Description B")]
    first = downloader(tmp_path, archive)
    first.options.transfer.output = "shared.%(ext)s"
    saved = first.fetch(items)
    sentinel = tmp_path / "shared.description"
    sentinel.write_text("Unrelated user notes")
    current = downloader(tmp_path, archive, jobs=jobs, assets=AssetOptions(write_description=True))
    current.options.transfer.overwrite = True
    with pytest.raises(OmniError, match="overlapping output paths"):
        current.fetch(items)
    assert sentinel.read_text() == "Unrelated user notes"
    assert [Path(result.path).read_bytes() for result in saved] == [payloads["/a"], payloads["/b"]]
    assert len(requests) == 2
    assert not (tmp_path / "a.description").exists()
    assert not (tmp_path / "b.description").exists()


def test_new_sidecar_cannot_replace_another_items_archived_main(archive_web, tmp_path):
    base, requests, payloads = archive_web
    archive = tmp_path / "archive.sqlite"
    existing = media(base, "a", "description")
    first = downloader(tmp_path, archive)
    first.options.transfer.output = "conflict.description"
    saved = first.fetch_one(existing)
    new = replace(media(base, "b", description="New description"), id="conflict")
    current = downloader(tmp_path, archive, assets=AssetOptions(write_description=True))
    current.options.transfer.overwrite = True
    with pytest.raises(OmniError, match="overlapping output paths"):
        current.fetch([existing, new])
    assert Path(saved.path).read_bytes() == payloads["/a"]
    assert requests == ["/a"]
    assert not (tmp_path / "conflict.mp4").exists()


def test_archive_recovery_checks_actual_reserved_sidecar_path(archive_web, tmp_path):
    base, requests, _ = archive_web
    archive = tmp_path / "archive.sqlite"
    original = media(base, description="Public description")
    downloader(tmp_path / "old", archive).fetch_one(original)
    protected = tmp_path / "old" / "a.description"
    protected.write_text("Reserved destination")
    current = downloader(tmp_path / "new", archive, assets=AssetOptions(write_description=True),
                         reserved_paths=[str(protected)])
    current.options.transfer.overwrite = True
    with pytest.raises(OmniError, match="reserved path"):
        current.fetch_one(original)
    assert protected.read_text() == "Reserved destination"
    assert requests == ["/a"]
