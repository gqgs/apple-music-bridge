"""Fresh-process tests: the shared pytest GUI masks missing Qt application setup."""

import json
import os
import subprocess
import sys

import pytest
from test_bridge_artwork import image_bytes


@pytest.mark.parametrize("mode", ["baseline", "bridge", "existing"])
def test_short_lived_image_threads_have_stable_qt_owner(tmp_path, mode):
    for fmt in ("PNG", "JPEG", "WEBP"):
        (tmp_path / fmt).write_bytes(image_bytes(fmt))
    code = r"""
import json, sys, threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from PySide6.QtCore import QCoreApplication, qInstallMessageHandler
from apple_music_player.artwork import decode
from apple_music_player.bridge.artwork import png
import apple_music_player.bridge.__main__ as entry
messages = []
qInstallMessageHandler(lambda kind, context, text: messages.append(text))
mode, directory = sys.argv[1:]
files = [Path(directory, f).read_bytes() for f in ('PNG', 'JPEG', 'WEBP')]
existing = QCoreApplication(['existing']) if mode == 'existing' else None
barrier = threading.Barrier(4)
def image_work(i):
    barrier.wait()
    for j in range(6):
        image = decode(files[(i+j)%3])
        assert not image.isNull()
        assert png(image).startswith(b'\x89PNG')
def workload():
    for generation in range(3):
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(image_work, range(4)))
if mode == 'baseline':
    workload()
else:
    class Actor:
        def __init__(self, factory):
            self.cancel = threading.Event()
            assert QCoreApplication.instance() is not None
            assert QCoreApplication.instance().thread().isCurrentThread()
            if existing is not None:
                assert QCoreApplication.instance() is existing
        def close(self): pass
    class Server:
        def __init__(self, *args): pass
        def serve_forever(self, **kwargs): workload()
        def server_close(self): pass
    entry.Actor, entry.Server = Actor, Server
    entry.auth_file = lambda *args: {'username':'test','key':'synthetic'}
    sys.argv = ['bridge', '--auth-file', 'unused']
    assert entry.main() == 0
print(json.dumps({'affinity_warnings':sum('QObject::moveToThread' in m for m in messages),
                  'other_warnings':sum('QObject::moveToThread' not in m for m in messages)}))
"""
    env = dict(os.environ)
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    # A QCoreApplication must not attempt to load any GUI platform plugin.
    env["QT_QPA_PLATFORM"] = "intentionally-unavailable"
    result = subprocess.run(
        [sys.executable, "-c", code, mode, str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    report = json.loads(result.stdout.splitlines()[-1])
    if mode == "baseline":
        assert report["affinity_warnings"] > 0
    else:
        assert report == {"affinity_warnings": 0, "other_warnings": 0}
