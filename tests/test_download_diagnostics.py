"""Source-grounded amdl v0.1.4 failure transcripts; all payloads are synthetic."""

import threading

import pytest

from apple_music_player.downloader import Downloader, Summary
from apple_music_player.model import PlayerError, Track


@pytest.mark.parametrize(
    ("transcript", "category"),
    [
        ("load Config failed: PRIVATE_PAYLOAD", "config"),
        ("Failed to get token.", "catalog-token"),
        (
            "Queue 1 of 1: Song->Failed to get song response.\n"
            "Failed to rip song: 401 Unauthorized\nnull",
            "catalog-401",
        ),
        ("Failed to rip song: 403 Forbidden", "catalog-403"),
        ("Failed to rip song: 404 Not Found", "catalog-404"),
        ("Failed to rip album: 429 Too Many Requests", "catalog-429"),
        ("Failed to rip song: PRIVATE_PAYLOAD", "catalog"),
        ("Error connecting to lite-server: PRIVATE_PAYLOAD", "wrapper"),
        ("Warning: failed to query lite-server /status: PRIVATE_PAYLOAD", "wrapper"),
        ("⚠ Failed to extract info from manifest: PRIVATE_PAYLOAD", "manifest"),
        ("Failed to run v4: PRIVATE_PAYLOAD\nError detected, exiting...", "download"),
        ("Defragment failed: PRIVATE_PAYLOAD", "finalize"),
        ("Unavailable\nnull", "unavailable"),
    ],
)
def test_source_diagnostics_fragmented_and_private(transcript, category):
    summary = Summary()
    data = (transcript + "\n").encode()
    for offset in range(0, len(data), 7):
        summary.feed(data[offset : offset + 7])
    assert (summary.error or summary.warning) == category
    assert "PRIVATE_PAYLOAD" not in repr(vars(summary))


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_realistic_failed_process_is_drained_and_sanitized(tmp_path, stream, exit_code):
    binary = tmp_path / "amdl-fixture"
    binary.write_text(f"""#!/usr/bin/env python3
import sys
output = sys.{stream}
print("PRIVATE_PAYLOAD" * 10000, file=output)
print("Queue 1 of 1: Song->Failed to get song response.", file=output)
print("Failed to rip song: 401 Unauthorized", file=output)
print("=======  [✔ ] Completed: 0/0  |  [⚠ ] Warnings: 0  |  [✖ ] Errors: 0  =======", file=output)
print("null", file=output)
sys.exit({exit_code})
""")
    binary.chmod(0o700)
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(PlayerError, match=r"\[catalog-401\]") as error:
        Downloader(str(binary), stage, backend="lite").run(
            Track("us", "12345", "Fixture"), "lossless", threading.Event()
        )
    assert "PRIVATE_PAYLOAD" not in str(error.value)
    assert not list(stage.iterdir())


def test_non_error_payload_and_stderr_cannot_supply_media():
    summary = Summary(accept_paths=False)
    summary.feed(b'{"token":"Failed to run v4: PRIVATE_PAYLOAD"}\n')
    summary.feed(b'[{"path":"/outside", "song":"PRIVATE_PAYLOAD"}]\n')
    assert summary.path is None and summary.error is None
    assert "PRIVATE_PAYLOAD" not in repr(vars(summary))
