# Rootless runtime and local authentication

The bridge reuses the original managed runtime, installer and downloader contracts.
Wrapper source is `56a7dcd726f2c2ef1d4420d6747ba0bafe215fee`; amdl v0.1.4 source is
`452ba0b672d49afffca3f57b0c2c4670a316e2ca`. Artifact IDs, SHA-256 values and the
recorded expiry are centralized in `backend_pins.py`. Native amdl audio finalization
does not require MP4Box/GPAC. ffprobe is required for our media verification.

`setup --archive ... --accept-runtime` validates bounded ZIP entries, rejects
traversal/symlinks/devices, checks immutable hashes and atomically installs in a
private directory. It never imports archived account state, replaces an existing
installation or runs binaries. Existing installs are verified and retained.
Artifact acquisition requires explicit user action; no mutable latest fallback.

`login` uses the local TTY, never HTTP or piped credential input. Password and 2FA
input disable terminal echo with restoration on success, errors and interruption.
The wrapper receives a sealed memfd through `LITE_ARGS_FILE=/proc/self/fd/...`;
credentials are not in argv or persistent argument files. Python strings cannot
promise secure erasure. The upstream parser imposes a 56 UTF-8-byte password limit
and disallows outer whitespace/newlines; no unsafe fallback is attempted.

Only source-defined status events are accepted from drained wrapper output. The
2FA FIFO is opened privately before submission becomes available. Its existence
satisfies the upstream file-existence check; the overall 180-second login deadline
continues while waiting for terminal input. Cancellation, failure and timeout
close descriptors, stop only the owned process group and restore the old session.
Interrupted login recovery and `.player-login-ok` receipts remain compatible.
Terminal reads remain bounded and keep polling even after partial input followed
by Ctrl-D; empty EOF cancels. Stored device/session tokens are protected by filesystem permissions, not encrypted.

`serve` starts an existing signed-in runtime, checks local readiness and supervises
its lifetime. An open port/local saved session does not prove current account media
access. The unauthenticated lite API binds only 127.0.0.1:12340. A private exclusive
runtime lock prevents duplicate management. SIGINT/SIGTERM shutdown reaps owned
children; another program's listener is never killed. Namespace/mount denials are
reported without privilege escalation or OS policy changes.

The old `apple_music_player.runtime_child` module path is intentionally retained.
Existing runtime directories and credentials are reused without automatic moves;
see README migration instructions. The bridge-only image codec uses Pillow and
needs no QCoreApplication or Qt plugin initialization.

`status` takes the runtime ownership lock when available and verifies installation
hashes. The existing store constructor can prune interrupted `install-*` staging;
it does not recover or modify login transactions/session files. If another process
owns the runtime, status reports that and probes only the local health endpoint.
