"""Construct and terminate a real MPV, many times, and survive it.

Not collected by `discover` (which only takes `test*.py`) -- it is the payload
for the `windows-stress` CI leg, and it is a *stress* rather than a test: it
asserts almost nothing, because the failure it hunts does not raise. It aborts
the interpreter:

    Fatal Python error: _PySemaphore_Wakeup: parking_lot: ReleaseSemaphore
    failed (error: 6)

That was the Windows teardown corruption -- `stop()` closing the pipe handle
from one thread while an overlapped read was still pending on another. So the
process exit status *is* the assertion, and the only thing this file has to do
is reach the bad path often enough to matter.

**How often is often enough.** The abort was seen twice in roughly 1700
teardowns, about 1 in 850. The whole suite performs ~35 per run, so a green
suite was never evidence of anything, and re-running it until green was
exactly the wrong response. Cycle count is the entire value here.

    python tests/stress_teardown.py 400
    STRESS_CYCLES=400 python tests/stress_teardown.py

It runs everywhere, but only Windows exercises the code it was written for.
On POSIX it is still a useful leak and hang check -- thread counts and socket
files are asserted the same way -- just not the one that matters.

Its cheaper sibling is `pipe_transport_check.py`, which drives the same
teardown against a loopback pipe with no mpv at all, so it can do tens of
thousands of cycles in the time this takes to do hundreds. Run both: this one
covers the full `MPV.terminate()` path, with the event handler and process
supervision in place, which is the shape the aborts actually arrived in.
"""

import os
import sys
import threading
import time

from _harness import LIVE_OPTIONS, MPV_BINARY, python_mpv_jsonipc


def watchdog(seconds):
    """Fail loudly on a hang rather than waiting for the CI step to be killed.

    A deadlocked teardown is a plausible regression here, and a job killed by
    its own timeout reports no cycle count and no last-known state -- so say
    both and exit hard, since the threads holding it will never be joinable.
    """
    def bark():
        time.sleep(seconds)
        sys.stderr.write(
            "\nFAILED: no progress for %ds; teardown is stuck.\n" % seconds)
        sys.stderr.flush()
        os._exit(2)

    thread = threading.Thread(target=bark)
    thread.daemon = True
    thread.start()


def main():
    if MPV_BINARY is None:
        sys.stderr.write("FAILED: no mpv found (set MPV_BINARY).\n")
        return 1

    cycles = int(sys.argv[1] if len(sys.argv) > 1
                 else os.environ.get("STRESS_CYCLES", "200"))
    # Generous: mpv start dominates, and a slow runner is not a failure. The
    # watchdog is here for a wedged teardown, not for a slow one.
    watchdog(int(os.environ.get("STRESS_TIMEOUT", "1800")))

    baseline = threading.active_count()
    print("mpv: %s" % MPV_BINARY, flush=True)
    print("cycles: %d, baseline threads: %d" % (cycles, baseline), flush=True)

    started = time.time()
    for i in range(cycles):
        mpv = python_mpv_jsonipc.MPV(mpv_location=MPV_BINARY, **LIVE_OPTIONS)
        # Talk to it before tearing it down, so the reader thread is parked in
        # a real pending read at stop() rather than in whatever state a
        # never-used socket happens to be in. That parked read is the bug.
        mpv.command("get_property", "pause")
        mpv.terminate()

        if mpv.mpv_process.process.returncode is None:
            sys.stderr.write(
                "\nFAILED: cycle %d left mpv unreaped.\n" % i)
            return 1

        if i and i % 50 == 0:
            print("  %d/%d cycles, %d threads live, %.1fs"
                  % (i, cycles, threading.active_count(),
                     time.time() - started), flush=True)

    elapsed = time.time() - started
    leaked = threading.active_count() - baseline
    print("completed %d cycles in %.1fs (%.2fs each)"
          % (cycles, elapsed, elapsed / max(cycles, 1)), flush=True)
    print("threads: %d baseline, %d now" % (baseline, baseline + leaked),
          flush=True)

    if leaked > 0:
        # Every MPV is terminated above, so its two transport threads and its
        # event handler should be gone. Threads that accumulate mean teardown
        # is returning without finishing -- see issue #17.
        sys.stderr.write("FAILED: %d threads leaked across %d cycles.\n"
                         % (leaked, cycles))
        return 1

    print("STRESS SURVIVED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
