# apple-music-bridge

Local Apple Music → OpenSubsonic bridge for clients such as
[Supersonic](https://github.com/gqgs/supersonic). Supports lossless and Atmos files;
audio output/passthrough is handled by your client.

Requires **Python 3.11+, ffprobe, Linux x86_64**, and permitted unprivileged user
namespaces. No Qt or mpv required by the bridge. Never run it with sudo.

## Install

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

## Set up and sign in

New installations need the pinned [rootless runtime ZIP](https://github.com/WorldObservationLog/wrapper/actions/runs/41407950505).
Setup verifies its checksum and downloads pinned amdl; it does not sign in.

```sh
apple-music-bridge setup --archive /path/to/wrapper-lite-linux-x86_64.zip --accept-runtime
apple-music-bridge login
```

Login prompts locally for your account, hidden password and 2FA code. Credentials
must not go in command arguments or logs. Session tokens are private files, not encrypted.

## Run

```sh
apple-music-bridge serve --country br --quality lossless
```

Use `--quality atmos` to prefer spatial audio, falling back per track to lossless,
then AAC when a format is unavailable. `--quality lossless` prefers lossless, then
AAC. Mixed-format playlists do not require restarting the bridge.
Point your client at `http://127.0.0.1:4533`;
use the username/key from `~/.config/apple-music-bridge/auth.json` as client
credentials. Keep that file private. `apple-music-bridge status` checks setup.

**Already using the bridge?** Skip setup/login. Existing legacy runtime sessions
and bridge caches are reused in place. Keep your existing `--auth-file`, `--cache`
and custom `--runtime-root` paths. The old `python -m apple_music_player.bridge`
command still works. Use a fresh virtual environment to drop Qt and old entrypoints.

[Options, migration, tests and troubleshooting](docs/bridge-guide.md) ·
[Runtime and authentication details](docs/rootless-setup.md)
