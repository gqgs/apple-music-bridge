import http.client
import json
import ssl
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import pytest

from apple_music_player.bridge import lyrics
from apple_music_player.bridge.http import EXTENSIONS, encoded_response

TTML = """<tt xmlns="http://www.w3.org/ns/ttml" xml:lang="en"><body><div>
<p begin="00:01.250"><span begin="1.25s">Synthetic </span><span>line &amp; test</span></p>
<p begin="2s">Second line</p></div></body></tt>"""


def test_timed_syllables_and_protocol():
    result = lyrics.parse_ttml(TTML)
    assert result == [
        {
            "lang": "en",
            "synced": True,
            "line": [
                {"start": 1250, "value": "Synthetic line & test"},
                {"start": 2000, "value": "Second line"},
            ],
        }
    ]
    body, _ = encoded_response({"lyricsList": {"structuredLyrics": result}}, "xml")
    node = ET.fromstring(body).find(".//{http://subsonic.org/restapi}structuredLyrics")
    assert node.get("synced") == "true"
    assert list(node)[0].attrib == {"start": "1250", "value": "Synthetic line & test"}
    assert {"name": "songLyrics", "versions": [1]} in EXTENSIONS


@pytest.mark.parametrize(
    "value,expected",
    [
        ("00:00:01.005", 1005),
        ("12:34.56", 754560),
        ("250ms", 250),
        ("2m", 120000),
        ("1h", 3600000),
        ("bad", None),
        ("NaNs", None),
        ("-1s", None),
        ("99:99", None),
        ("9999999h", None),
    ],
)
def test_times(value, expected):
    assert lyrics.timestamp(value) == expected


def test_plain_and_partial_times():
    assert lyrics.parse_response({"data": [{"attributes": {"text": "First\nSecond"}}]}) == [
        {"lang": "und", "synced": False, "line": [{"value": "First"}, {"value": "Second"}]}
    ]
    result = lyrics.parse_ttml('<tt><body><p begin="1s">One</p><p>Two</p></body></tt>')[0]
    assert not result["synced"]
    assert all("start" not in line for line in result["line"])
    assert lyrics.parse_response({"data": []}) == []
    assert lyrics.parse_ttml("<tt><body><p/></body></tt>") == []


@pytest.mark.parametrize(
    "raw",
    [
        '<!DOCTYPE tt [<!ENTITY x "bad">]><tt/>',
        "<other/>",
        "<tt>",
        "x" * (lyrics.MAX_BYTES + 1),
    ],
)
def test_invalid_xml(raw):
    with pytest.raises((ValueError, ET.ParseError)):
        lyrics.parse_ttml(raw)


def test_private_tokens(tmp_path):
    token = tmp_path / "DEV_TOKEN"
    token.write_text("fixture-token\n")
    token.chmod(0o600)
    assert lyrics.read_token(tmp_path, "DEV_TOKEN") == "fixture-token"
    token.chmod(0o644)
    with pytest.raises(ValueError):
        lyrics.read_token(tmp_path, "DEV_TOKEN")
    token.chmod(0o600)
    (tmp_path / "MUSIC_TOKEN").symlink_to(token)
    with pytest.raises(ValueError):
        lyrics.read_token(tmp_path, "MUSIC_TOKEN")
    token.write_text("bad\nheader")
    with pytest.raises(ValueError):
        lyrics.read_token(tmp_path, "DEV_TOKEN")


def test_verified_origin_no_redirect(monkeypatch, tmp_path):
    for name in ("DEV_TOKEN", "MUSIC_TOKEN"):
        (tmp_path / name).write_text("fixture")
        (tmp_path / name).chmod(0o600)
    calls = []
    status = [200]

    class Connection:
        def __init__(self, host, timeout, context):
            assert host == "amp-api.music.apple.com"
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            assert timeout == 10

        def request(self, method, path, headers):
            calls.append(path)
            assert headers["Authorization"] == "Bearer fixture"
            assert headers["media-user-token"] == "fixture"

        def getresponse(self):
            self.status = status[0]
            return self

        def read(self, limit):
            assert limit == lyrics.MAX_BYTES + 1
            return json.dumps({"data": [{"attributes": {"ttmlLocalizations": TTML}}]}).encode()

        def close(self):
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    assert lyrics.fetch_lyrics(tmp_path, "br", "123")[0]["synced"]
    status[0] = 302
    with pytest.raises(ValueError):
        lyrics.fetch_lyrics(tmp_path, "br", "123")
    assert len(calls) == 2  # Redirect is never followed.
    status[0] = 404
    assert lyrics.fetch_lyrics(tmp_path, "br", "123") == []
    with pytest.raises(ValueError):
        lyrics.fetch_lyrics(tmp_path, "br", "../other")


def test_singleflight_cache_and_failure():
    calls = []
    gate = threading.Event()

    def fetch(*args):
        calls.append(args)
        gate.wait(1)
        return lyrics.parse_ttml(TTML)

    service = lyrics.Lyrics(fetch=fetch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(service.get, "br", "3") for _ in range(4)]
        time.sleep(0.05)
        gate.set()
        results = [job.result() for job in jobs]
    assert len(calls) == 1
    results[0][0]["line"].clear()
    assert service.get("br", "3")[0]["line"]

    def fail(*args):
        raise ssl.SSLError("secret upstream data")

    service = lyrics.Lyrics(fetch=fail)
    assert service.get("br", "1") == []
    assert service.get("br", "2") == []
    assert lyrics.Lyrics().get("br", "1") == []


def test_relative_container_and_span_timing():
    result = lyrics.parse_ttml(
        '<tt><body><div begin="10s"><p begin="2s">One</p>'
        '<p><span begin="4s">Two</span></p></div></body></tt>'
    )[0]
    # Untimed paragraph inherits its container's start, per TTML.
    assert [line["start"] for line in result["line"]] == [10000, 12000]
    result = lyrics.parse_ttml(
        '<tt><body><p><span begin="2s">Two</span><span begin="3s">three</span></p></body></tt>'
    )[0]
    assert result["line"][0]["start"] == 2000
    result = lyrics.parse_ttml(
        '<tt><body timeContainer="seq"><p begin="1s">One</p><p begin="2s">Two</p></body></tt>'
    )[0]
    assert not result["synced"]


@pytest.mark.parametrize("text", ["a\0b", "a\x1bb", "\ud800", "\ufffe"])
def test_plain_rejects_xml_invalid_characters(text):
    with pytest.raises(ValueError):
        lyrics.parse_response({"data": [{"attributes": {"text": text}}]})


def test_nested_sequential_timing_is_not_guessed():
    raw = (
        '<tt><body><p><span timeContainer="seq"><span dur="2s">one</span>'
        '<span begin="1s">two</span></span></p></body></tt>'
    )
    assert not lyrics.parse_ttml(raw)[0]["synced"]
