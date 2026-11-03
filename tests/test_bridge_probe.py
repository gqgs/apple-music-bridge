import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_artist_images import MBID, credits
from test_bridge import ALBUM, ARTIST, KEY
from test_bridge import bridge as _bridge
from test_bridge_artwork import URL, image_bytes

from apple_music_player.bridge import artist_images as images

bridge = _bridge


def probe_module():
    spec = importlib.util.spec_from_file_location(
        "bridge_probe", Path(__file__).resolve().parents[1] / "scripts/bridge_image_probe.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setup_probe(server, tmp_path, monkeypatch, check=False):
    auth = tmp_path / "private-auth.json"
    auth.write_text(json.dumps({"username": "bridge", "key": KEY}))
    auth.chmod(0o600)
    args = [
        "probe",
        "--auth-file",
        str(auth),
        "--port",
        str(server.server_port),
        "--cache",
        str(server.actor.core.cache.root),
    ]
    if check:
        args.append("--check-identity")
    monkeypatch.setattr(sys, "argv", args)
    server.actor.call("remember", [ARTIST, replace(ALBUM, artwork_url=URL)])
    monkeypatch.setattr(server.covers, "get", lambda *_: image_bytes("PNG"))
    return auth


def output(capsys, auth):
    text = capsys.readouterr().out
    assert KEY not in text and str(auth) not in text
    assert ARTIST.title not in text and URL not in text
    assert "am:br:" not in text and "http://" not in text and "https://" not in text
    return [json.loads(line) for line in text.splitlines()]


@pytest.mark.parametrize("mode", ["disabled", "success", "missing"])
def test_probe_real_bridge_album_and_portrait(bridge, tmp_path, monkeypatch, capsys, mode):
    server, _ = bridge
    auth = setup_probe(server, tmp_path, monkeypatch)
    if mode != "disabled":
        server.actor.core.artist_images = True
        server.artist_images = images.ArtistImages(tmp_path / "portraits")

        def resolve(*_):
            if mode == "missing":
                raise images.Missing("identity-missing")
            return image_bytes("PNG"), credits()

        monkeypatch.setattr(images, "resolve", resolve)
    assert probe_module().main() == 0
    rows = output(capsys, auth)
    album = next(row for row in rows if row["stage"] == "album_cover")
    assert album["http"] == 200 and album["decodes"] and not album["bridge_placeholder"]
    assert album["cache_header_count"] == 1 and not album["no_store"]
    if mode == "disabled":
        assert next(row for row in rows if row["stage"] == "artist_candidates")["portrait_ids"] == 0
    else:
        portrait = next(row for row in rows if row["stage"] == "artist_cover")
        assert portrait["http"] == (200 if mode == "success" else 503)
        assert portrait["decodes"] == (mode == "success")


@pytest.mark.parametrize("shape", ["list", "empty", "single", "unexpected"])
def test_probe_reports_identity_response_shape_without_public_ids_or_urls(
    bridge, tmp_path, monkeypatch, capsys, shape
):
    server, _ = bridge
    auth = setup_probe(server, tmp_path, monkeypatch, check=True)
    server.actor.core.artist_images = True
    server.artist_images = images.ArtistImages(tmp_path / "portraits")

    def resolve(*_):
        raise images.Missing("identity-missing")

    monkeypatch.setattr(images, "resolve", resolve)
    requests = []

    def metadata(url, stop):
        requests.append(url)
        assert "apiKey" not in url and KEY not in url
        row = {
            "resource": images.resources(ARTIST)[0],
            "relations": [{"target-type": "artist", "artist": {"id": MBID}}],
        }
        return {
            "list": {"urls": [row]},
            "empty": {"urls": []},
            "single": row,
            "unexpected": {"unknown": True},
        }[shape]

    monkeypatch.setattr(images, "metadata", metadata)
    assert probe_module().main() == 0
    rows = output(capsys, auth)
    result = next(row for row in rows if row["stage"] == "identity_provider")
    assert result["shape"] == ("list" if shape == "empty" else shape)
    assert result["matching_urls"] == (1 if shape in {"list", "single"} else 0)
    assert result["unique_artist_ids"] == (1 if shape in {"list", "single"} else 0)
    assert len(requests) == 1


def test_probe_missing_auth_reports_safe_stage_and_type(tmp_path, monkeypatch, capsys):
    auth = tmp_path / "personal-path-not-for-output"
    monkeypatch.setattr(sys, "argv", ["probe", "--auth-file", str(auth)])
    assert probe_module().main() == 1
    assert output(capsys, auth) == [
        {"stage": "authentication_file", "error_type": "FileNotFoundError"}
    ]
