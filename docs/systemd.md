# Automatic startup with systemd

Use a **user service**, never a root/system service. Run the normal setup and
interactive login once first, or keep your existing working runtime and session.
The service uses `serve`: it never installs the runtime, prompts for credentials,
or signs in automatically. Apple passwords, 2FA codes and tokens must not be put
in the unit, environment files or command arguments.

## Install and start

Activate the installed bridge's virtual environment and stop any manually running
bridge. Choose the same options you use with `serve`:

```sh
apple-music-bridge install-service --country br --quality lossless
systemctl --user daemon-reload
systemctl --user enable --now apple-music-bridge.service
systemctl --user status apple-music-bridge.service
```

The installer writes `$XDG_CONFIG_HOME/systemd/user/apple-music-bridge.service`
(default `~/.config/systemd/user/`). It does **not** run systemctl or enable/start
anything. It captures the current Python/virtualenv, ffprobe search directory and
absolute runtime, auth-file and cache paths, including legacy runtime detection
and custom XDG defaults. No shell startup files or activated shell are needed.
The virtualenv, editable checkout if applicable, and runtime must stay in place.
Use executable directories without colons and a Python path without `=`; the
installer rejects these unsupported search-path/launcher cases.

Pass your existing `--runtime-root`, `--auth-file`, `--cache`, `--cache-gib`,
`--country`, `--quality` and `--port` settings to `install-service` as needed.
`--diagnostics` enables the same credential-free aggregate diagnostics as `serve`.
The installer does not read, regenerate, move or copy account/client credentials.
An existing unit is left untouched unless you explicitly use `--replace`.

If you override `XDG_CONFIG_HOME` only in your shell, your systemd user manager may
search a different directory. Enable the installed unit by its absolute path:
`systemctl --user enable --now /absolute/config/systemd/user/apple-music-bridge.service`.

## Login versus boot

Without lingering, `enable` starts the service with your user manager, usually at
first login. It may stop after your last logout. To start at boot without logging
in and keep it running after logout, explicitly enable lingering for your account:

```sh
loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
```

This may require administrator authorization and affects **all** your enabled user
services. It is optional; the installer never changes it. Boot startup requires
the virtualenv, home and runtime paths to be accessible before login. An encrypted
or login-mounted home may make login startup the appropriate choice instead.
There is no dependency on a desktop, sound server, or display. A user service
cannot reliably wait on the system's `network-online.target`; network availability
and actual Apple media access still need checking when used.

## Control, update and troubleshoot

```sh
systemctl --user stop apple-music-bridge.service
systemctl --user start apple-music-bridge.service
systemctl --user restart apple-music-bridge.service
journalctl --user -u apple-music-bridge.service -n 50 --no-pager
```

The bridge remains at `http://127.0.0.1:4533` unless you selected another port.
Its existing private auth file supplies your client's username/key. A systemd
`active` state means the bridge process is running, not that the runtime, Apple
session, network or playback has been verified. Account/session expiry still
requires local interactive login; stop the service before login and start it
afterward. Keep the same custom paths on those commands. If the owned runtime
stops while the HTTP process remains alive, restart the service after addressing
the cause. The unit does not run an account-health or playback watchdog.

The unit retries a failed bridge process after 10 seconds, up to five starts in
two minutes. This avoids an endless loop for missing setup, expired local state,
busy ports, or host-policy failures. After fixing a startup failure:

```sh
systemctl --user reset-failed apple-music-bridge.service
systemctl --user start apple-music-bridge.service
```

If `systemctl --user` cannot connect to the bus, use a normal logged-in systemd
user session, not sudo. Do not relax namespace or other security policy to work
around an administrator's denial. The service retains the existing rootless
runtime requirements, ownership checks and private-file permissions. `KillMode=mixed`
lets the bridge stop its owned runtime cleanly; systemd kills remaining descendants
if shutdown exceeds 30 seconds. It never kills unrelated port owners.

When changing options or moving/recreating the virtualenv, review the installed
unit, then regenerate it with all desired options:

```sh
apple-music-bridge install-service --replace --country br --quality atmos
systemctl --user daemon-reload
systemctl --user restart apple-music-bridge.service
```

Do not add secrets to the unit or enable raw runtime debug output. Normal logs
use the bridge's existing fixed diagnostics; journal access follows your host's
policy. No restrictive systemd namespace/mount sandbox is added because the
managed rootless launcher needs those operations.

## Remove

```sh
systemctl --user disable --now apple-music-bridge.service
rm -- "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/apple-music-bridge.service"
systemctl --user daemon-reload
```

This leaves runtime, session, auth and cache files intact. If you enabled lingering
only for this service, consider `loginctl disable-linger "$USER"`; it affects your
other user services too. Remove any custom drop-ins separately if you created them.
