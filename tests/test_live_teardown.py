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
import tempfile
import threading
import time
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

    def test_a_failed_start_does_not_leave_mpv_running(self):
        # The failure path orphaned MPV exactly as the success path did, and
        # worse: MPV.__init__ retries construction up to start_retries times,
        # so a machine where MPV wedges without opening its pipe left five
        # live processes, each holding the caller's stdout.
        #
        # The endpoint poll is answered False so the start "times out" while
        # MPV is genuinely running, and sleep is a no-op so that costs
        # microseconds instead of the real ten seconds.
        spawned = []
        real_popen = python_mpv_jsonipc.subprocess.Popen

        def record(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            spawned.append(process)
            return process

        with mock.patch.object(python_mpv_jsonipc.subprocess, "Popen", record), \
                mock.patch.object(python_mpv_jsonipc, "_ipc_endpoint_ready",
                                  return_value=False), \
                mock.patch.object(python_mpv_jsonipc.time, "sleep",
                                  lambda seconds: None):
            with self.assertRaises(python_mpv_jsonipc.MPVProcessError):
                python_mpv_jsonipc.MPVProcess(
                    "/tmp/mpv-failed-start-test", MPV_BINARY, **LIVE_OPTIONS)

        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(
            spawned[0].returncode,
            "a start that failed left MPV running and unreaped")

    def test_a_quit_callback_that_terminates_does_not_join_its_own_thread(self):
        # quit_callback runs on the reader thread, and terminate() defaults to
        # join=True -- so the reader was asked to join itself. RuntimeError
        # escaped run(), which skipped the internal terminate(join=False)
        # after it and leaked the event handler.
        mpv = start_mpv()
        escaped = []
        original_hook = threading.excepthook
        threading.excepthook = lambda args: escaped.append(
            "{0}: {1}".format(args.exc_type.__name__, args.exc_value))
        self.addCleanup(setattr, threading, "excepthook", original_hook)

        mpv.quit_callback = mpv.terminate
        mpv.mpv_process.process.kill()

        deadline = time.time() + 5
        while mpv.mpv_inter.socket.is_alive() and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.2)

        self.assertEqual(escaped, [])

    def test_the_process_is_reaped_not_merely_signalled(self):
        mpv = start_mpv()
        process = mpv.mpv_process.process

        mpv.terminate()

        self.assertIsNotNone(process.poll())


class StopWaitingTest(unittest.TestCase):
    """No mpv here: a stub is the only way to hold SIGTERM open on demand."""

    class _Stubborn:
        """Ignores terminate(); only kill() gets through."""

        def __init__(self):
            self.killed = False
            self.waits = []

        def terminate(self):
            pass

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            self.waits.append(timeout)
            if self.killed:
                return -9
            raise python_mpv_jsonipc.subprocess.TimeoutExpired("mpv", timeout)

    def _process(self, socket_name="absent.sock"):
        process = python_mpv_jsonipc.MPVProcess.__new__(
            python_mpv_jsonipc.MPVProcess)
        process.process = self._Stubborn()
        process.ipc_socket = os.path.join(tempfile.mkdtemp(), socket_name)
        return process

    def test_a_timeout_of_none_does_not_wait_at_all(self):
        process = self._process()

        process.stop(timeout=None)

        self.assertEqual(process.process.waits, [])
        self.assertFalse(process.process.killed)

    def test_a_timeout_waits_then_escalates_to_kill(self):
        process = self._process()

        process.stop(timeout=0)

        self.assertTrue(process.process.killed)
        self.assertEqual(process.process.waits, [0, 0])

    @unittest.skipIf(os.name == "nt", "POSIX socket files only")
    def test_an_unlink_failure_that_is_not_the_race_is_not_swallowed(self):
        # Only FileNotFoundError means "the other thread got there first".
        # Swallowing every OSError would hide a permission problem or a path
        # that is not the socket we think it is.
        process = self._process()
        with mock.patch("os.remove", side_effect=PermissionError("nope")):
            with self.assertRaises(PermissionError):
                process.stop(timeout=None)


@requires_mpv
class NonBlockingTerminateTest(unittest.TestCase):
    def test_terminate_join_false_asks_the_process_not_to_wait(self):
        # join=False is the non-blocking teardown, and the path
        # _quit_callback takes from the reader thread. Waiting up to ten
        # seconds there would block a caller who asked not to be blocked.
        mpv = start_mpv()
        self.addCleanup(mpv.terminate)

        with mock.patch.object(mpv.mpv_process, "stop") as stop:
            mpv.terminate(join=False)

        # call_args_list[0], not call_args: terminate() re-enters itself.
        # Closing the socket wakes the reader into _quit_callback, which calls
        # terminate(join=False) again, so the *last* call is always the
        # internal one whichever way the caller asked.
        self.assertEqual(stop.call_args_list[0], mock.call(timeout=None))

    def test_terminate_by_default_still_waits(self):
        mpv = start_mpv()
        self.addCleanup(mpv.terminate)

        with mock.patch.object(mpv.mpv_process, "stop") as stop:
            mpv.terminate()

        self.assertEqual(stop.call_args_list[0], mock.call(timeout=5))


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
