"""Keep the rootless process group scoped to the desktop player's lifetime."""

import os
import signal
import subprocess
import sys
import time


def main():
    parent, lock_fd, args_fd = map(int, sys.argv[1:4])
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    if os.getppid() != parent:
        return 1
    fds = tuple(fd for fd in (lock_fd, args_fd) if fd >= 0)
    # Stay in the supervisor's new process group, never the unrelated processes'.
    child = subprocess.Popen(sys.argv[4:], pass_fds=fds)
    while child.poll() is None and not stopped and os.getppid() == parent:
        time.sleep(0.1)
    if child.poll() is not None and not stopped:
        return child.returncode
    os.killpg(os.getpgrp(), signal.SIGINT)
    time.sleep(0.15)
    os.killpg(os.getpgrp(), signal.SIGKILL)
    return 1


if __name__ == "__main__":
    sys.exit(main())
