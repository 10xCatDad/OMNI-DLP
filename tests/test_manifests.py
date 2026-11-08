"""HLS parsing contract fixtures, independent of network and FFmpeg."""
import pytest

from omni_dlp.manifests import parse_hls
from omni_dlp.models import OmniError

BASE = "https://media.example.test/path/master.m3u8?token=fixture"
MASTER = '''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="stereo",NAME="English, original",LANGUAGE="en",DEFAULT=YES,AUTOSELECT=YES,CHANNELS="2",URI="audio/en.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="stereo",NAME="French",LANGUAGE="fr",DEFAULT=NO,AUTOSELECT=YES,URI="/audio/fr.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=3000000,AVERAGE-BANDWIDTH=2400000,RESOLUTION=1280x720,FRAME-RATE=29.97,CODECS="avc1.64001f,mp4a.40.2",AUDIO="stereo",SUBTITLES="subs"
video/720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,FRAME-RATE=60,CODECS="avc1.640028,mp4a.40.2",AUDIO="stereo"
video/1080.m3u8
'''


def test_master_variants_and_external_audio_groups():
    items = parse_hls(MASTER, BASE)
    assert [item.format_id for item in items] == ["hls-720p", "hls-1080p", "hls-audio-stereo-en", "hls-audio-stereo-fr"]
    video, _, english, french = items
    assert video.url == "https://media.example.test/path/video/720.m3u8"
    assert (video.width, video.height, video.fps, video.tbr) == (1280, 720, 29.97, 2400)
    assert (video.vcodec, video.acodec) == ("avc1.64001f", "none")
    assert video.metadata["audio_group"] == english.metadata["audio_group"] == "stereo"
    assert video.metadata["subtitles_group"] == "subs"
    assert (english.vcodec, english.acodec, english.language) == ("none", "mp4a.40.2", "en")
    assert english.metadata["name"] == "English, original"
    assert english.metadata["default"] is True
    assert english.metadata["channels"] == "2"
    assert french.url == "https://media.example.test/audio/fr.m3u8"
    assert french.metadata["default"] is False
    assert all(item.ext == "m3u8" and item.protocol == "hls" for item in items)


def test_inband_audio_is_not_mislabeled_video_only():
    data = MASTER.replace(',URI="audio/en.m3u8"', '')
    items = parse_hls(data, BASE)
    assert items[0].acodec == "mp4a.40.2"
    assert len(items) == 3  # Two video variants and one externally available rendition.


def test_muxed_and_audio_only_variants():
    text = '''#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=640x360,CODECS="avc1.42001e,mp4a.40.2"
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=128000,CODECS="mp4a.40.2"
audio.m3u8
'''
    video, audio = parse_hls(text, BASE)
    assert (video.vcodec, video.acodec) == ("avc1.42001e", "mp4a.40.2")
    assert (audio.vcodec, audio.acodec) == ("none", "mp4a.40.2")
    assert audio.format_id == "hls-audio-128k"


def test_dedup_and_same_quality_unique_ids_are_stable():
    text = '''#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
one.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
one.m3u8#ignored
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720
two.m3u8
'''
    items = parse_hls(text, BASE)
    assert [item.format_id for item in items] == ["hls-720p", "hls-720p-2"]
    assert [item.to_dict() for item in parse_hls(text, BASE)] == [item.to_dict() for item in items]


def test_cross_origin_strips_all_unrecognized_headers():
    text = MASTER.replace("video/720.m3u8", "https://cdn.example.test/video.m3u8")
    items = parse_hls(text, BASE, headers={"Authorization": "fixture", "Cookie": "fixture", "X-Api-Key": "fixture", "Referer": "https://example.test/?token=fixture", "User-Agent": "fixture-agent"})
    assert items[0].headers == {"User-Agent": "fixture-agent", "Accept-Encoding": "identity"}
    assert items[1].headers["Authorization"] == "fixture"
    assert items[2].headers["Cookie"] == "fixture"


def test_media_playlist_unknown_codecs_and_liveness():
    data = '\ufeff#EXTM3U\r\n#EXT-X-TARGETDURATION:10\r\n#EXTINF:10,\r\nsegment.ts\r\n#EXT-X-ENDLIST\r\n'
    item, = parse_hls(data, BASE)
    assert item.url == BASE
    assert item.format_id == "hls"
    assert item.metadata["is_live"] is False
    assert item.vcodec == item.acodec == ""
    live, = parse_hls(data.replace('#EXT-X-ENDLIST', ''), BASE)
    assert live.metadata["is_live"] is True


@pytest.mark.parametrize("line", ['#EXT-X-KEY:METHOD=AES-128,URI="key.bin"', '#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.apple.streamingkeydelivery",URI="skd://key"'])
def test_encrypted_media_and_session_keys_fail_explicitly(line):
    with pytest.raises(OmniError, match="Encrypted HLS"):
        parse_hls('#EXTM3U\n' + line + '\n#EXTINF:10,\nfile.ts\n', BASE)


def test_clear_key_reset_is_accepted():
    item, = parse_hls('#EXTM3U\n#EXT-X-KEY:METHOD=NONE\n#EXTINF:2,\nfile.ts\n', BASE)
    assert item.protocol == "hls"


@pytest.mark.parametrize("uri", ["file:///etc/example", "data:application/octet-stream,example", "ftp://example.test/file", "https://user:password@example.test/file", "javascript:example()", "https://example.test:bad/file", "{$unbound}/file.m3u8"])
def test_unsafe_and_unresolved_variant_uris_are_rejected(uri):
    with pytest.raises(OmniError):
        parse_hls('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=123\n' + uri, BASE)


def test_unsafe_segment_and_map_uris_are_rejected_before_delegation():
    for body in ('#EXTINF:2,\nfile:///example', '#EXT-X-MAP:URI="file:///example"\n#EXTINF:2,\nsegment.ts'):
        with pytest.raises(OmniError):
            parse_hls('#EXTM3U\n' + body, BASE)


@pytest.mark.parametrize("body", [
    '#EXT-X-STREAM-INF:BANDWIDTH=1',
    '#EXT-X-STREAM-INF:RESOLUTION=1280x720\nfile.m3u8',
    '#EXT-X-STREAM-INF:BANDWIDTH=1,BANDWIDTH=2\nfile.m3u8',
    '#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS="unterminated\nfile.m3u8',
    '#EXT-X-STREAM-INF:BANDWIDTH=1,\nfile.m3u8',
    '#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS="avc1"bad\nfile.m3u8',
    '#EXT-X-STREAM-INF:BANDWIDTH=1,AUDIO="missing"\nfile.m3u8',
    '#EXT-X-MEDIA:TYPE=AUDIO,NAME="track",URI="audio.m3u8"',
    '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="track",DEFAULT=perhaps,URI="audio.m3u8"',
])
def test_malformed_master_fails_actionably(body):
    with pytest.raises(OmniError):
        parse_hls('#EXTM3U\n' + body, BASE)


@pytest.mark.parametrize("attributes", ['BANDWIDTH=nan', 'BANDWIDTH=-1', 'BANDWIDTH=1,FRAME-RATE=inf', 'BANDWIDTH=1,RESOLUTION=720', 'BANDWIDTH=1,RESOLUTION=0x720'])
def test_invalid_numeric_attributes(attributes):
    with pytest.raises(OmniError, match="Invalid HLS"):
        parse_hls('#EXTM3U\n#EXT-X-STREAM-INF:' + attributes + '\nfile.m3u8', BASE)


def test_unknown_codecs_are_not_falsely_marked_absent():
    item, = parse_hls('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS="future.video,future.audio"\nfile.m3u8', BASE)
    assert item.vcodec == item.acodec == ""


def test_iframe_only_master_is_not_playable():
    with pytest.raises(OmniError, match="no playable"):
        parse_hls('#EXTM3U\n#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=1,URI="iframe.m3u8"', BASE)


@pytest.mark.parametrize("data", ["<html>not a playlist</html>", "#EXTM3U\n#\x00invalid", '#EXTM3U\n#' + 'x' * 65536, '#EXTM3U\n' + 'x' * (8 * 1024 * 1024)])
def test_document_validation_and_bounds(data):
    with pytest.raises(OmniError):
        parse_hls(data, BASE)


def test_mixed_master_and_media_playlist_is_rejected():
    with pytest.raises(OmniError, match="mixes"):
        parse_hls('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nvideo.m3u8\n#EXTINF:2,\nsegment.ts', BASE)


def test_extreme_resolution_is_an_actionable_error():
    with pytest.raises(OmniError, match="resolution"):
        parse_hls('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=' + '9' * 5000 + 'x720\nvideo.m3u8', BASE)


def test_audio_renditions_and_segments_cannot_mix():
    with pytest.raises(OmniError, match="mixes"):
        parse_hls('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="one",URI="audio.m3u8"\n#EXTINF:2,\nsegment.ts', BASE)


def test_default_audio_cannot_disable_autoselection():
    with pytest.raises(OmniError, match="automatic selection"):
        parse_hls('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="one",DEFAULT=YES,AUTOSELECT=NO,URI="audio.m3u8"', BASE)
