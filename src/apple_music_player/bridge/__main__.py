"""Explicit local launch; never installs a runtime or signs in."""

import argparse
import os
import signal
from pathlib import Path

from ..model import PlayerError
from .core import Actor
from .http import ProtocolCore, Server, auth_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-file", type=Path, required=True)
    parser.add_argument(
        "--init-auth", action="store_true", help="Create private random bridge credentials and exit"
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        / "apple-music-bridge",
    )
    parser.add_argument("--cache-gib", type=int, default=10)
    parser.add_argument("--country", default="br")
    parser.add_argument("--quality", choices=("lossless", "atmos"), default="lossless")
    parser.add_argument("--port", type=int, default=4533)
    parser.add_argument(
        "--start-runtime",
        type=Path,
        metavar="EXISTING_RUNTIME_ROOT",
        help="Explicitly start an already installed/signed-in managed runtime; close the GUI first",
    )
    args = parser.parse_args()
    os.umask(0o077)

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    actor = server = None
    try:
        if args.init_auth:
            auth_file(args.auth_file, create=True)
            print("Created private bridge credentials. Read the file locally; do not share it.")
            return 0
        if not 1 <= args.port <= 65535 or not 1 <= args.cache_gib <= 1024:
            raise ValueError()
        credentials = auth_file(args.auth_file)
        actor = Actor(
            lambda cancel: ProtocolCore(
                args.cache,
                args.cache_gib * 1024**3,
                args.country,
                args.quality,
                runtime_root=args.start_runtime,
                cancel=cancel,
            )
        )
        server = Server(actor, credentials, args.port, args.quality)
        print(f"Local bridge: http://127.0.0.1:{args.port} · {args.quality} · placeholder artwork")
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, PlayerError):
        print("Cannot start bridge. Check auth file, cache/runtime ownership, port and setup.")
        return 1
    finally:
        if actor:
            actor.cancel.set()
        if server:
            server.server_close()
        if actor:
            actor.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
