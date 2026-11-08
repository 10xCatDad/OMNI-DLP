"""Full native selection and registry integration without an upstream dependency."""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest

from omni_dlp import Format, Media, OmniError
from omni_dlp.cli import main
from omni_dlp.engine import Downloader, Options
from omni_dlp.extractors.common import ExtractorContext
from omni_dlp.transfer import TransferOptions


@pytest.fixture
def native_server():
    requests = []
    data = {"/high.mp4": b"higher quality fixture", "/low.mp4": b"lower quality fixture",
            "/json": b'{"ok":true}', "/large": b"x" * 100,
            "/master.m3u8": b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=640x360,CODECS="avc1.4d001e,mp4a.40.2"\nlow.m3u8\n'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            path = self.path.split("?")[0]
            if path == "/old/master.m3u8":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/new/master.m3u8")
                self.end_headers()
                return
            if path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/json")
                self.end_headers()
                return
            content = data.get("/master.m3u8" if path == "/new/master.m3u8" else path)
            self.send_response(200 if content is not None else 404)
            self.send_header("Content-Length", str(len(content or b"")))
            self.end_headers()
            self.wfile.write(content or b"")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", data, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def item(base):
    return Media(base + "/page", id="video", title="A video", kind="video", source_url=base + "/page",
                 metadata={"extractor": "fixture", "archive_id": "fixture:video"}, formats=[
                     Format(base + "/low.mp4", "360", "mp4", vcodec="avc1", acodec="aac", height=360),
                     Format(base + "/high.mp4", "720", "mp4", vcodec="avc1", acodec="aac", height=720)])


def test_native_builtin_discovery_selects_and_downloads_best(native_server, tmp_path):
    base, data, requests = native_server
    client = Downloader(Options(transfer=TransferOptions(directory=str(tmp_path)), plugins=False))
    with patch("omni_dlp.extractors.extract_builtin", return_value=[item(base)]):
        discovered = client.inspect(base + "/page")
    result = client.fetch(discovered)[0]
    assert Path(result.path).read_bytes() == data["/high.mp4"]
    assert result.media.metadata["format_id"] == "720"
    assert requests[0][0] == "/high.mp4"
    assert item(base).requested_formats == []


def test_multiple_format_outputs_have_distinct_default_names(native_server, tmp_path):
    base, data, _ = native_server
    client = Downloader(Options(transfer=TransferOptions(directory=str(tmp_path)), format="360,720"))
    results = client.fetch([item(base)])
    assert len({r.path for r in results}) == 2
    assert {Path(r.path).read_bytes() for r in results} == {data["/low.mp4"], data["/high.mp4"]}


def test_selected_format_receipt_survives_signed_url_refresh(native_server, tmp_path):
    base, _, requests = native_server
    client = Downloader(Options(transfer=TransferOptions(directory=str(tmp_path)), format="720"))
    first = item(base)
    first.formats[-1].url += "?temporary=first"
    initial = client.fetch([first])[0]
    refreshed = item(base)
    refreshed.formats[-1].url += "?temporary=second"
    repeated = client.fetch([refreshed])[0]
    assert repeated.skipped and repeated.sha256 == initial.sha256
    assert len(requests) == 1
    assert "temporary" not in Path(initial.path + ".omni.json").read_text()


def test_format_listing_never_downloads_or_emits_control_characters(native_server, capsys):
    base, _, requests = native_server
    media = replace(item(base), title="unsafe\x1b[31m\nname")
    with patch.object(Downloader, "inspect", return_value=[media]):
        assert main([base + "/page", "-F"]) == 0
    output = capsys.readouterr().out
    assert "720" in output and "360" in output and "ID\tEXT" in output
    assert "\x1b" not in output and not requests


def test_explicit_selection_simulation_and_redaction(native_server, capsys):
    base, _, requests = native_server
    media = item(base)
    media.formats[0].url += "?credential=hidden"
    media.formats[0].headers = {"Authorization": "hidden"}
    with patch.object(Downloader, "inspect", return_value=[media]):
        assert main([base + "/page", "-f", "360", "--simulate"]) == 0
    output = capsys.readouterr().out
    record = json.loads(output)
    assert record["requested_formats"][0]["format_id"] == "360"
    assert "hidden" not in output and not requests


def test_http_extractor_context_bounds_and_scopes_headers(native_server):
    base, _, requests = native_server
    ctx = ExtractorContext(base + "/page", headers={"Authorization": "secret", "X-Api-Key": "secret"})
    assert ctx.get_json(base + "/json") == {"ok": True}
    assert requests[-1][1]["Authorization"] == "secret"
    assert ctx.get_json(base + "/redirect") == {"ok": True}
    assert "Authorization" not in requests[-1][1] and "X-Api-Key" not in requests[-1][1]
    with pytest.raises(OmniError, match="limit"):
        ExtractorContext(base, max_document=10).get_bytes(base + "/large")
    with pytest.raises(OmniError, match="HTTP 404"):
        ctx.get_json(base + "/missing")
    with pytest.raises(OmniError):
        ctx.get_bytes("file:///private-file")


def test_hls_master_candidates_are_expanded(native_server):
    base, _, _ = native_server
    media = Media(base + "/page", id="hls", kind="video", formats=[
        Format(base + "/master.m3u8", "master", "m3u8", protocol="hls")])
    client = Downloader(Options(plugins=False))
    with patch("omni_dlp.extractors.extract_builtin", return_value=[media]):
        found = client.inspect(base + "/page")[0]
    assert len(found.formats) == 1
    assert found.formats[0].height == 360
    assert found.formats[0].url == base + "/low.m3u8"


def test_single_item_api_selects_representation(native_server, tmp_path):
    base, data, _ = native_server
    client = Downloader(Options(transfer=TransferOptions(directory=str(tmp_path)), format="360"))
    result = client.fetch_one(item(base))
    assert Path(result.path).read_bytes() == data["/low.mp4"]
    with pytest.raises(OmniError, match="multiple"):
        Downloader(Options(format="360,720")).fetch_one(item(base))


def test_selected_formats_scope_global_credentials_without_source_url(native_server, tmp_path):
    base, _, requests = native_server
    media = item(base)
    media.url = "https://page.example.invalid/watch"
    media.source_url = ""
    options = Options(format="720", transfer=TransferOptions(directory=str(tmp_path),
                      headers={"Authorization": "page-secret", "X-Api-Key": "page-secret"}))
    result = Downloader(options).fetch([media])[0]
    assert result.media.source_url == "https://page.example.invalid/watch"
    assert "Authorization" not in requests[-1][1] and "X-Api-Key" not in requests[-1][1]


def test_failed_manifests_cannot_become_webpage_downloads(native_server, tmp_path):
    base, _, requests = native_server
    media = Media(base + "/json", kind="video", formats=[Format(base + "/missing", "hls", "m3u8", protocol="hls")])
    client = Downloader(Options(transfer=TransferOptions(directory=str(tmp_path)), plugins=False))
    with patch("omni_dlp.extractors.extract_builtin", return_value=[media]):
        extracted = client.inspect(base + "/json")
    with pytest.raises(OmniError, match="No playable"):
        client.fetch(extracted)
    assert all(path != "/json" for path, _ in requests)


def test_archive_hits_cannot_corrupt_old_media_with_manifest(native_server, tmp_path, capsys):
    base, data, _ = native_server
    old = tmp_path / "old"
    new = tmp_path / "new"
    archive = tmp_path / "archive.db"
    arguments = [base + "/high.mp4", "--direct", "-P", str(old), "-o", "old.mp4",
                 "--download-archive", str(archive)]
    assert main(arguments) == 0
    target = old / "old.mp4"
    assert main([base + "/high.mp4", "--direct", "-P", str(new), "--download-archive", str(archive),
                 "--manifest", str(target)]) == 1
    assert target.read_bytes() == data["/high.mp4"]
    assert "actual media output" in capsys.readouterr().err


def test_merge_archive_identity_includes_effective_container(native_server, tmp_path):
    from omni_dlp.models import DownloadResult
    import hashlib
    base, _, _ = native_server
    video = Format(base + "/high.mp4", "v", "mp4", vcodec="avc1", acodec="none")
    audio = Format(base + "/audio.m4a", "a", "m4a", vcodec="none", acodec="aac")
    media = replace(item(base), formats=[video, audio])
    archive = str(tmp_path / "archive.db")
    first = Downloader(Options(archive=archive, transfer=TransferOptions(directory=str(tmp_path)), merge_output_format="mkv"))
    second = Downloader(Options(archive=archive, transfer=TransferOptions(directory=str(tmp_path)), merge_output_format="mp4"))

    def merged(item, options, *, container):
        path = tmp_path / ("result." + container)
        payload = container.encode()
        path.write_bytes(payload)
        return DownloadResult(replace(item, ext=container), str(path), len(payload), hashlib.sha256(payload).hexdigest())
    with patch("omni_dlp.merge.download_merged", side_effect=merged) as merge:
        assert first.fetch([media])[0].media.ext == "mkv"
        assert second.fetch([media])[0].media.ext == "mp4"
        assert merge.call_count == 2


def test_document_final_url_and_target_headers(native_server):
    base, _, requests = native_server
    context = ExtractorContext("https://origin.invalid/page", headers={"Authorization": "origin-secret"})
    data, final, headers = context.get_document(base + "/json", headers={"X-Api-Key": "target-secret"})
    assert json.loads(data) == {"ok": True} and final == base + "/json"
    assert requests[-1][1]["X-Api-Key"] == "target-secret"
    assert "Authorization" not in requests[-1][1]
    assert headers["X-Api-Key"] == "target-secret"
    _, final, headers = context.get_document(base + "/redirect", headers={"X-Api-Key": "target-secret"})
    assert final == base.replace("127.0.0.1", "localhost") + "/json"
    assert "X-Api-Key" not in headers and "X-Api-Key" not in requests[-1][1]


def test_selected_representation_metadata_keeps_logical_identity(native_server):
    base, _, _ = native_server
    media = item(base)
    media.metadata.update(filename="original.webm", md5="original")
    media.formats[0].metadata.update(filename="selected.mp4", md5="selected", archive_id="wrong", resource_id="wrong")
    selected = Downloader(Options(format="360")).prepare([media])[0]
    assert selected.metadata["filename"] == "selected.mp4" and selected.metadata["md5"] == "selected"
    assert selected.metadata["archive_id"] == "fixture:video"
    assert selected.metadata["resource_id"] == "fixture:video|format:360"


def test_hls_expansion_cache_keeps_target_header_variants_separate(native_server):
    base, _, requests = native_server
    media = Media(base + "/page", kind="video", formats=[
        Format(base + "/master.m3u8", "one", "m3u8", protocol="hls", headers={"X-Token": "one"}),
        Format(base + "/master.m3u8", "two", "m3u8", protocol="hls", headers={"X-Token": "two"})])
    result = Downloader()._expand_manifests([media], base + "/page")[0]
    assert len(requests) == 2
    assert [fmt.headers["X-Token"] for fmt in result.formats] == ["one", "two"]


def test_ignore_errors_continues_after_unavailable_format(native_server, tmp_path, capsys):
    base, data, _ = native_server
    bad = item(base)
    good = replace(item(base), id="audio", kind="audio", formats=[
        Format(base + "/low.mp4", "audio", "mp4", vcodec="none", acodec="aac")])
    with patch.object(Downloader, "inspect", return_value=[bad, good]):
        assert main([base + "/page", "-f", "ba", "--ignore-errors", "-P", str(tmp_path)]) == 1
    outputs = list(tmp_path.glob("*.mp4"))
    assert len(outputs) == 1 and outputs[0].read_bytes() == data["/low.mp4"]
    assert "No formats match" in capsys.readouterr().err
    with patch.object(Downloader, "inspect", return_value=[bad, good]):
        assert main([base + "/page", "-f", "ba", "--ignore-errors", "--simulate"]) == 1
    assert json.loads(capsys.readouterr().out)["id"] == "audio"


def test_redirected_hls_resolves_relative_variants_at_final_location(native_server):
    base, _, requests = native_server
    media = Media(base + "/page", kind="video", formats=[
        Format(base + "/old/master.m3u8", "master", "m3u8", protocol="hls", headers={"X-Token": "private"})])
    found = Downloader()._expand_manifests([media], base + "/page")[0]
    assert found.formats[0].url == base.replace("127.0.0.1", "localhost") + "/new/low.m3u8"
    assert "X-Token" not in requests[-1][1]
    assert "X-Token" not in found.formats[0].headers
