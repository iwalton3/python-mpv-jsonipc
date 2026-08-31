"""What ``terminate()`` guarantees about the mpv process it started.

Reported by jellyfin-mpv-shim, 2026-08-31: an integration leg printed its
results, exited cleanly, and then its parent hung forever reading the leg's
stdout. The orphan was an mpv with PPID 1 still holding the pipe the parent
was blocked on -- ``/proc/<mpv>/fd/1`` named the exact inode.

Two separate facts produced that, and they are tested separately here because
only one of them was fixed:

* ``stop()`` sent SIGTERM and returned without waiting, so ``terminate()``
  could return while mpv was still running. That is now a wait with a kill
  escalation, and it is what these tests pin.
* mpv inherits the caller's stdout and stderr, because ``close_fds`` only
  covers fds above 2. That is *unchanged by default* -- callers have had
  mpv's output on their terminal for years -- and opt-out via
  ``discard_output``. The default is pinned below as characterization, not
  as approval.

The intermittency is the tell: it is a race between mpv's SIGTERM handling
and the caller exiting, so a run where the orphan does not appear proves
nothing. These assert the postcondition instead of trying to lose the race.

A third fact arrived with the fix rather than before it, which is why it is
tested here too: making ``stop()`` wait turned a long-latent check-then-act
race on the socket unlink into a reliable one, because the wait ends on the
same MPV exit that wakes the reader into a second ``terminate()``. It was
found by ``stress_teardown.py`` at ~50 cycles, not by this file -- a reminder
that the cheap tests here cover postconditions, not interleavings.
"""

import os
import unittest
from unittest import mock

from _harness import MPV_BINARY, LIVE_OPTIONS, python_mpv_jsonipc, requires_mpv

#: The fd-inheritance assertions read /proc, so they are Linux-only. The
#: waiting assertions above them are not, and run everywhere.
has_proc = unittest.skipUnless(os.path.exists("/proc/self/fd"),
                               "needs /proc to see the child's fds")


def start_mpv(**extra):
    options = dict(LIVE_OPTIONS)
    options.update(extra)
    return python_mpv_jsonipc.MPV(mpv_location=MPV_BINARY, **options)


@requires_mpv
class TerminateWaitsTest(unittest.TestCase):
    def test_terminate_does_not_return_while_mpv_may_still_be_alive(self):
        mpv = start_mpv()
        process = mpv.mpv_process.process

        mpv.terminate()

        # returncode is only set by a wait(). Before the fix nothing ever
        # called one, so this was None however long mpv had been dead --
        # which is the whole complaint: the caller had no way to know.
        self.assertIsNotNone(
            process.returncode,
            "terminate() returned without waiting for mpv; a caller that "
            "exits here orphans an mpv still holding its stdout")

    @unittest.skipIf(os.name == "nt", "POSIX socket files only")
    def test_the_socket_removal_survives_losing_the_race(self):
        # Two threads reach MPVProcess.stop(): the caller's terminate(), and
        # the reader's quit_callback -> terminate(join=False), woken by the
        # very MPV exit that stop() now waits for. So they overlap by
        # construction, and the loser finds the socket already unlinked.
        #
        # The window itself cannot be hit on demand, but its outcome can:
        # exists() answering True for a file that is gone is exactly what the
        # loser sees. Check-then-act raises FileNotFoundError here.
        mpv = start_mpv()
        process = mpv.mpv_process
        mpv.terminate()
        self.assertFalse(os.path.exists(process.ipc_socket))

        with mock.patch("os.path.exists", return_value=True):
            process.stop()

    def test_the_process_is_reaped_not_merely_signalled(self):
        mpv = start_mpv()
        process = mpv.mpv_process.process

        mpv.terminate()

        self.assertIsNotNone(process.poll())


@requires_mpv
@has_proc
class InheritedOutputTest(unittest.TestCase):
    def _child_stdio(self, mpv):
        pid = mpv.mpv_process.process.pid
        return (os.readlink("/proc/{0}/fd/1".format(pid)),
                os.readlink("/proc/{0}/fd/2".format(pid)))

    def test_mpv_inherits_the_callers_stdout_and_stderr_by_default(self):
        # Characterization. This is the behaviour that let an orphaned mpv
        # hold a test runner's pipe open, and it is still the default because
        # changing it would silently take mpv's output away from every caller
        # who currently sees it.
        mpv = start_mpv()
        self.addCleanup(mpv.terminate)
        ours = (os.readlink("/proc/self/fd/1"), os.readlink("/proc/self/fd/2"))

        self.assertEqual(self._child_stdio(mpv), ours)

    def test_discard_output_keeps_mpv_off_the_callers_pipes(self):
        mpv = start_mpv(discard_output=True)
        self.addCleanup(mpv.terminate)

        stdout, stderr = self._child_stdio(mpv)

        self.assertEqual(stdout, os.devnull)
        self.assertEqual(stderr, os.devnull)

    def test_discard_output_is_not_passed_on_to_mpv(self):
        # mpv has a real --quiet; it must keep working as an mpv option, which
        # is why this keyword is not called that. If discard_output ever leaks
        # into the argv, mpv rejects it and nothing starts at all.
        mpv = start_mpv(discard_output=True, quiet=True)
        self.addCleanup(mpv.terminate)

        self.assertIn("--quiet=yes", mpv.mpv_process.argv)
        self.assertEqual(
            [a for a in mpv.mpv_process.argv if "discard" in a], [])


if __name__ == "__main__":
    unittest.main()
