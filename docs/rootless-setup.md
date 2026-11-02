# Managed rootless setup (stacked on MVP PR #1)

This change adds an optional managed backend; the external classic configuration stays selectable and uses its original downloader/config contract. No runtime starts automatically. The application remembers the selected backend, and cached tracks can play without the service. Uncached lite downloads require the managed service to be ready.

## Local flow

Open **Backend setup**, choose **Managed rootless lite**, and follow the three steps:

1. Open the pinned upstream GitHub Actions run and download **wrapper-lite-linux-x86_64**. GitHub may require browser sign-in. Check the installation consent box and select that ZIP. The app verifies its SHA-256, extracts only the native rootless launcher/system files, downloads and verifies amdl v0.1.4, and commits the installation atomically. It never installs the privileged launcher, imports account files, or executes binaries during installation.
2. Enter your account and password in the local dialog and click **Sign in using installed runtime**. Enter the six-digit verification code when requested. Login cancellation/failure restores the prior local session. The source imposes a 56 UTF-8 byte password limit for safe 2FA handling; outer whitespace/newlines cannot be represented by its argument-file parser. The app rejects those cases instead of truncating or changing your password.
3. Click **Start / retry managed service**. The app binds lite to `127.0.0.1:12340`, supervises it, and stops its process group on quit. Select lite as your playback backend, close setup, and Retry a previously failed queue entry. Use Settings for your HDMI device as before.

If the host denies user namespaces/mounts, setup reports that limitation. The app never changes sysctls, permissions on system paths, container security settings, or launches an elevated GUI. Use classic or consult your administrator; QEMU is not included. If port 12340 is occupied, its existing owner is left alone. Missing media dependencies are listed; the app does not install OS packages.

## Exact supply-chain pins

All pins are in `src/apple_music_player/backend_pins.py`. They were verified against upstream source/API metadata on 2026-11-02, not by running the proprietary runtime.

| Component | Identity | SHA-256 / source |
| --- | --- | --- |
| Native wrapper | WorldObservationLog/wrapper `56a7dcd726f2c2ef1d4420d6747ba0bafe215fee` | Source-reviewed `wrapper-lite-rootless.c`, `lite/lite_main.cpp`, `lite/auth.cpp`, `lite/logger.h` |
| Native ZIP | Run `41407950505`, artifact `13124456520`, `wrapper-lite-linux-x86_64` | `98d6a0d8c5118ce96b97abb0561623aa87feb771b258d280dd52cfa8a21f2c7f` |
| amdl Linux amd64 v0.1.4 | Release asset `669065376`; source `452ba0b672d49afffca3f57b0c2c4670a316e2ca` | `1a3a4000edd74be98efccf7f611b97d47ef822d8e059805c6aaaef98f0477747` |
| External classic | downloader `12fff0caead84c264abdb02878d43b1773e13368`; wrapper `4459548fb3c0f96dcb974755eb4a5a0791fbbc74` | Existing user-managed installation, unchanged |

The wrapper artifact is currently unexpired but **expires 2027-01-31**. It is not a public release asset: [upstream run](https://github.com/WorldObservationLog/wrapper/actions/runs/41407950505), [artifact metadata](https://api.github.com/repos/WorldObservationLog/wrapper/actions/artifacts/13124456520). Its ZIP is imported locally because GitHub Actions downloads may need a browser-authenticated user. The app does not request a GitHub token. If the artifact is unavailable/expired, setup is blocked until a reviewed replacement identity/hash is committed. It never substitutes the mutable classic “latest” release. Upstream's current CMake build also references Dobby `master`; a reproducible source-build fallback was therefore not invented.

The downloader URL is the versioned [v0.1.4 release](https://github.com/zhaarey/apple-music-downloader/releases/tag/v0.1.4). SHA verification enforces content identity even if upstream changes a tag/asset. TLS verification remains on; HTTP downgrade redirects are rejected. The archive has bounded size/count and rejects traversal, symlinks and special files. Installation stores a private per-file integrity manifest checked before starting. An existing valid pinned install is reused; changed installs are rejected rather than overwritten. No runtime upgrade/migration mechanism is included in this first version.

No upstream/proprietary binary is committed or redistributed here. The wrapper repository labels its code MIT, while its bundled Apple/Android libraries have separate provenance and rights. The consent screen names that origin; installing it is a local user decision.

## Authentication and status semantics

`LITE_ARGS_FILE` is source-verified to read one argument per line and override ordinary arguments inside the chroot. The app uses a sealed, 0600 Linux memfd, inherited through the rootless launcher and opened as `/proc/self/fd/N`; passwords never enter process arguments or a persistent argument file. If memfd/sealing is unavailable, login fails closed. The upstream 2FA file channel is served by a private FIFO at `rootfs/data/2fa.txt`, not a regular code file. An owned read/write descriptor keeps upstream’s fopen/fclose existence checks from blocking or discarding the code before its final fscanf read. The FIFO is unlinked on success/cancel/exit, and credential widgets and descriptors are cleared/closed. Python/Qt cannot promise forensic zeroization or protection against OS memory inspection/swap; this is not a credential vault. No plaintext password persistence fallback exists.

Session tokens/device state live under `$XDG_DATA_HOME/apple-music-player/installed/rootfs/data` (default `~/.local/share/apple-music-player/...`) with 0700 directories and a 077 child umask. They are private **but not encrypted**. Passwords are not saved there by this integration. The external runtime owns its account database; do not share this directory. No raw stdout/stderr or HTTP payloads are shown, saved or attached to diagnostics. A bounded parser consumes only exact, source-defined login-complete/2FA/namespace-denial events; other output is discarded.

A process or open port only establishes “running.” `/status` reports a region list and does not validate account tokens. “Locally signed in” requires this app to observe upstream login completion and newly created nonempty token files; exit zero alone is insufficient because the rootless parent discards child status. “Ready” additionally requires the app-owned service to answer the expected health schema. Account/token expiry and media entitlement are still checked by actual downloads, which may fail with sanitized errors. CORS/debug mode are never enabled. The API is unauthenticated and accessible to other local processes; it is not exposed to the LAN.

The runtime lock is inherited by its owned processes, preventing duplicate instances. A small Python supervisor tracks the desktop parent and terminates the rootless group after parent death; it never targets an external classic wrapper. Start, cancel, login and health work run off the Qt thread. Installation/network cancellation can wait up to the current 20-second read timeout. Login has a 180-second overall deadline; upstream gives 60 seconds for 2FA. Cancel/quit closes the memfd, stops only the owned group and rolls back an unfinished login. Restart recovers interrupted login transactions without deleting a previous session. No system security policy is weakened.

## Compatibility and validation

amdl v0.1.4 uses nested `general.lite-server`, `media`, `paths`, `metadata` and `convert` settings. Its `--json` remains a final mixed-output array with `path`, not a streaming API. The adapter passes an explicit private `--config`, disables lyrics/artwork/conversion, and shares the existing output validation and ffprobe labels. It does not reuse the classic flat config or call updater flags.

Tests exercise synthetic archive install/rollback/hash failures, unsafe ZIP entries, private modes, denied host policies, memfd failure, fake rootless login/2FA, cancellation/restoration, parent death, zero-exit authentication failure, healthy-but-unauthenticated service, health loss, busy ports, backend selection, a fake v0.1.4 CLI contract and Qt setup smoke. The earlier media-format/cache/playback tests still run. `docs/setup-smoke.png` shows an offscreen setup dialog with no credentials or live runtime.

**Still needs real Arch acceptance:** artifact import, actual rootless namespace/mount permissions, inherited memfd visibility inside the chroot, real Apple login/2FA, token refresh, live downloads, and mpv/HDMI output. During implementation no proprietary runtime was installed/executed, no account was used, and no media was downloaded. Source review plus mock tests establish an integration contract, not real-hardware compatibility.
