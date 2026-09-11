"""Trusted PID-1 renewal supervisor, executed by unshare, using stdlib only.

PID namespace init adopts orphaned descendants regardless of setsid/double fork.
Only ECHILD after natural zero exits is success. Exiting init on timeout kills
the entire namespace in the kernel; the outer unshare parent reaps init before
returning FORCED. This is lifecycle containment, not filesystem isolation.
"""

from __future__ import annotations

import math
import os
import signal
import sys

INCONCLUSIVE = 78
FORCED = 79
WAIT_ALL = 0x40000000  # Linux __WALL includes clone children without SIGCHLD.


def main() -> int:
    if os.getpid() != 1:
        return INCONCLUSIVE
    args = sys.argv[1:]
    if args == ["--check"]:
        return 0
    if len(args) < 4 or args[0] != "--timeout" or args[2] != "--":
        return INCONCLUSIVE
    timeout = float(args[1])
    if not math.isfinite(timeout) or timeout <= 0:
        return INCONCLUSIVE
    signal.signal(signal.SIGALRM, lambda *_: os._exit(FORCED))
    signal.setitimer(signal.ITIMER_REAL, timeout)
    child = os.fork()
    if child == 0:
        try:
            # The parent supplies reviewed native argv, never shell text.
            os.execvpe(args[3], args[3:], os.environ)  # nosec B606
        except (OSError, ValueError):
            os._exit(INCONCLUSIVE)
    native_reaped = False
    successful = True
    while True:
        try:
            pid, status = os.waitpid(-1, WAIT_ALL)
        except ChildProcessError:
            break
        native_reaped |= pid == child
        successful &= os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    signal.setitimer(signal.ITIMER_REAL, 0)
    return 0 if native_reaped and successful else INCONCLUSIVE


if __name__ == "__main__":
    sys.exit(main())
