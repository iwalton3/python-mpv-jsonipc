"""What happens when mpv will not start.

**Most of this file exists to be changed.** The error handling here is due to
be replaced with something that names the option mpv rejected and refuses to
retry a start that cannot succeed. Pinning the current behaviour first is what
makes that change reviewable: the diff will show precisely which observable
things moved, rather than leaving it to be discovered by a downstream user on
an unmaintained project.

So: assert what it does, not what it should do. In particular the exact
exception message and the retry count are load-bearing here only because
somebody out there may be matching on them.
"""

import os
import subprocess
import time
import unittest
from unittest import mock

from _harness import (LIVE_OPTIONS, MPV_BINARY, python_mpv_jsonipc,
                      requires_mpv)


@requires_mpv
class BadOptionTest(unittest.TestCase):
    """mpv exits immediately when handed an option it does not have. This is
    the exact case the shim hits with a build-gated option such as
    `--input-gamepad` on an mpv built without SDL2 gamepad support.

    **These assertions were rewritten deliberately** when start-failure
    diagnosis landed. What they said before is preserved in
    `PreDiagnosisBehaviourTest` below, reachable with
    `diagnose_start_failures=False`, because that is the behaviour downstream
    projects have been living with.
    """

    def start_with_bad_option(self, **overrides):
        options = dict(LIVE_OPTIONS)
        options.update(dict(mpv_location=MPV_BINARY,
                            start_retries=3,
                            start_retry_delay_ms=50))
        options.update(overrides)
        options["definitely_not_an_mpv_option"] = "yes"
        return python_mpv_jsonipc.MPV(**options)

    def test_the_error_names_the_option_mpv_refused(self):
        with self.assertRaises(python_mpv_jsonipc.MPVProcessError) as caught:
            self.start_with_bad_option()
        self.assertEqual(caught.exception.bad_option,
                         "definitely-not-an-mpv-option")
        self.assertIn("definitely-not-an-mpv-option", str(caught.exception))

    def test_it_is_still_an_mpv_error(self):
        # The compatibility promise. Downstream catches MPVError and nothing
        # finer; a new type that escaped those handlers would be a break
        # dressed up as an improvement.
        with self.assertRaises(python_mpv_jsonipc.MPVError):
            self.start_with_bad_option()

    def test_it_carries_mpvs_exit_status_and_its_own_words(self):
        with self.assertRaises(python_mpv_jsonipc.MPVProcessError) as caught:
            self.start_with_bad_option()
        self.assertEqual(caught.exception.returncode, 1)
        self.assertIn("Error parsing option", caught.exception.log_output)
        self.assertFalse(caught.exception.retryable)

    def test_it_stops_instead_of_spending_every_retry(self):
        # Exactly one real start attempt plus one diagnostic probe, and no
        # second attempt. Counted by shape rather than by total, because the
        # probe goes through Popen too (subprocess.run uses it), so a bare
        # count of 2 would also be satisfied by two starts and no probe.
        with mock.patch.object(python_mpv_jsonipc.subprocess, "Popen",
                               wraps=subprocess.Popen) as popen:
            with self.assertRaises(python_mpv_jsonipc.MPVProcessError):
                self.start_with_bad_option(start_retries=3)
        argvs = [call.args[0] for call in popen.call_args_list]
        starts = [a for a in argvs if "--version" not in a]
        probes = [a for a in argvs if "--version" in a]
        self.assertEqual(len(starts), 1, "MPV was started more than once")
        self.assertEqual(len(probes), 1, "the diagnosis ran more than once")

    def test_it_does_not_pay_the_retry_delay_it_cannot_use(self):
        started = time.perf_counter()
        with self.assertRaises(python_mpv_jsonipc.MPVProcessError):
            self.start_with_bad_option(start_retries=3,
                                       start_retry_delay_ms=3000)
        self.assertLess(time.perf_counter() - started, 3.0)

    def test_the_diagnosis_does_not_bind_the_ipc_socket(self):
        # The probe re-runs MPV with the same --input-ipc-server. `--version`
        # makes it exit before serving, but if that ever stopped being true
        # the diagnosis would squat on the endpoint the real MPV is about to
        # open -- turning a diagnosable failure into a mysterious one.
        socket_path = "/tmp/diagnosis-probe-{0}".format(os.getpid())
        if os.name == "nt":
            self.skipTest("POSIX socket paths only")
        with self.assertRaises(python_mpv_jsonipc.MPVProcessError):
            self.start_with_bad_option(ipc_socket=socket_path)
        self.assertFalse(os.path.exists(socket_path))

    def test_mpvs_own_words_survive_even_when_no_option_is_named(self):
        # Not every refusal is an option error -- a missing DLL or an
        # unusable VO reads the same way to a user: something changed and
        # there is nothing to go on. On Windows especially there is no
        # console they could have seen it in. So whatever MPV said is
        # attached even when the diagnosis cannot name an option.
        with mock.patch.object(python_mpv_jsonipc, "_diagnose_start_failure",
                               return_value=(None, "mpv: some other refusal")):
            with self.assertRaises(python_mpv_jsonipc.MPVProcessError) as caught:
                self.start_with_bad_option(start_retries=2)
        self.assertEqual(str(caught.exception),
                         "MPV process retry limit reached.")
        self.assertEqual(caught.exception.log_output, "mpv: some other refusal")

    def test_a_failure_it_cannot_explain_still_retries_and_reports_as_before(self):
        # The diagnosis is not always conclusive -- mpv may have died for a
        # reason it did not write down. That path must behave exactly as it
        # always did, or an unexplained flake becomes a hard failure.
        with mock.patch.object(python_mpv_jsonipc, "_diagnose_start_failure",
                               return_value=(None, "nothing conclusive")):
            with mock.patch.object(python_mpv_jsonipc.subprocess, "Popen",
                                   wraps=subprocess.Popen) as popen:
                with self.assertRaises(python_mpv_jsonipc.MPVError) as caught:
                    self.start_with_bad_option(start_retries=3)
                self.assertEqual(str(caught.exception),
                                 "MPV process retry limit reached.")
                self.assertEqual(popen.call_count, 3)


@requires_mpv
class PreDiagnosisBehaviourTest(unittest.TestCase):
    """`diagnose_start_failures=False` -- what every release before 1.3 did.

    Kept as a supported escape hatch, and tested, because "you can turn the
    new behaviour off" is worth nothing if nobody checks that the switch
    still reaches the old code.
    """

    def start_with_bad_option(self, **overrides):
        options = dict(LIVE_OPTIONS)
        options.update(dict(mpv_location=MPV_BINARY,
                            start_retries=3,
                            start_retry_delay_ms=50,
                            diagnose_start_failures=False))
        options.update(overrides)
        options["definitely_not_an_mpv_option"] = "yes"
        return python_mpv_jsonipc.MPV(**options)

    def test_it_raises_the_retry_limit_message_and_names_no_option(self):
        with self.assertRaises(python_mpv_jsonipc.MPVError) as caught:
            self.start_with_bad_option()
        self.assertEqual(str(caught.exception),
                         "MPV process retry limit reached.")
        self.assertNotIn("definitely", str(caught.exception))

    def test_every_retry_is_spent_on_a_start_that_cannot_succeed(self):
        with mock.patch.object(python_mpv_jsonipc.subprocess, "Popen",
                               wraps=subprocess.Popen) as popen:
            with self.assertRaises(python_mpv_jsonipc.MPVError):
                self.start_with_bad_option(start_retries=3)
            self.assertEqual(popen.call_count, 3)

    def test_the_configured_delay_is_paid_after_every_failure(self):
        started = time.perf_counter()
        with self.assertRaises(python_mpv_jsonipc.MPVError):
            self.start_with_bad_option(start_retries=3,
                                       start_retry_delay_ms=300)
        self.assertGreaterEqual(time.perf_counter() - started, 0.9)


@requires_mpv
class MissingBinaryTest(unittest.TestCase):
    def test_a_missing_mpv_binary_is_not_retried_and_is_not_an_mpv_error(self):
        # `except MPVError` in the retry loop does not catch this, so it
        # escapes on the first attempt. jellyfin-mpv-shim depends on exactly
        # this to tell "no mpv installed" from "mpv rejected an option".
        with mock.patch.object(python_mpv_jsonipc.subprocess, "Popen",
                               wraps=subprocess.Popen) as popen:
            with self.assertRaises(FileNotFoundError):
                python_mpv_jsonipc.MPV(
                    mpv_location=os.path.join(os.sep, "nonexistent", "mpv"),
                    start_retries=5, start_retry_delay_ms=50, **LIVE_OPTIONS)
            self.assertEqual(popen.call_count, 1)


@requires_mpv
class NoServerTest(unittest.TestCase):
    def test_attaching_to_a_socket_nothing_is_listening_on_raises(self):
        # start_mpv=False means "something else owns the process". If the
        # endpoint is not there, the transport raises out of the constructor
        # rather than waiting -- but *which* exception is a real divergence
        # between the two transports, found by the Windows CI leg rather than
        # by reading the code:
        #
        #   UnixSocket    -> socket.connect fails -> OSError, immediately.
        #   WindowsSocket -> CreateFile fails -> five attempts a second apart
        #                    -> MPVError("Cannot connect to pipe."), which is
        #                    NOT an OSError.
        #
        # Pinned per platform rather than widened to `Exception`, because the
        # difference is the thing worth knowing: code that catches OSError
        # around this constructor works on Linux and not on Windows.
        expected = (python_mpv_jsonipc.MPVError if os.name == "nt"
                    else OSError)
        with self.assertRaises(expected):
            python_mpv_jsonipc.MPV(start_mpv=False,
                                   ipc_socket="definitely-no-mpv-here"
                                   if os.name == "nt"
                                   else "/tmp/definitely-no-mpv-here")


@requires_mpv
class QuitCallbackTest(unittest.TestCase):
    def test_it_fires_when_mpv_goes_away(self):
        import threading
        fired = threading.Event()
        mpv = python_mpv_jsonipc.MPV(mpv_location=MPV_BINARY,
                                     quit_callback=fired.set, **LIVE_OPTIONS)
        self.addCleanup(lambda: None)
        try:
            mpv.command("quit")
        except Exception:
            pass  # the socket may die before the reply arrives
        self.assertTrue(fired.wait(10), "quit_callback never fired")


@requires_mpv
class SuccessfulStartTest(unittest.TestCase):
    def test_a_good_start_spawns_exactly_one_process(self):
        with mock.patch.object(python_mpv_jsonipc.subprocess, "Popen",
                               wraps=subprocess.Popen) as popen:
            mpv = python_mpv_jsonipc.MPV(mpv_location=MPV_BINARY,
                                         **LIVE_OPTIONS)
            self.addCleanup(mpv.terminate)
            self.assertEqual(popen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
