"""Run both dev targets in independent POSIX process groups; stop both on any exit."""

import os
import signal
import subprocess
import sys
import time


def supervise(make: str) -> int:
    children: list[subprocess.Popen] = []
    interrupted = 0

    def stop(signum, _frame):
        nonlocal interrupted
        interrupted = signum

    def broadcast(signum):
        for child in children:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    previous = {
        sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        for target in ("dev-backend", "dev-frontend"):
            children.append(subprocess.Popen([make, target], start_new_session=True))
        while not interrupted:
            for child in children:
                code = child.poll()
                if code is not None:
                    return code if code >= 0 else 128 - code
            time.sleep(0.05)
        return 128 + interrupted
    except OSError as exc:
        print(f"Unable to start development services: {exc}", file=sys.stderr)
        return 1
    finally:
        # Leaders may already have exited while their descendants are still alive.
        broadcast(signal.SIGTERM)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if all(child.poll() is not None for child in children):
                break
            time.sleep(0.05)
        broadcast(signal.SIGKILL)
        for child in children:
            child.wait()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(supervise(sys.argv[1] if len(sys.argv) > 1 else "make"))
