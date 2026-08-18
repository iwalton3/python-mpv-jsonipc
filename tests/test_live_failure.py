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
    `--input-gamepad` on an mpv built without SDL2 gamepad support."""

    def start_with_bad_option(self, **overrides):
        options = dict(LIVE_OPTIONS)
        options.update(dict(mpv_location=MPV_BINARY,
                            start_retries=3,
                            start_retry_delay_ms=50))
        options.update(overrides)
        options["definitely_not_an_mpv_option"] = "yes"
        return python_mpv_jsonipc.MPV(**options)

    def test_it_raises_mpv_error_with_the_retry_limit_message(self):
        with self.assertRaises(python_mpv_jsonipc.MPVError) as caught:
            self.start_with_bad_option()
        self.assertEqual(str(caught.exception),
                         "MPV process retry limit reached.")

    def test_the_error_says_nothing_about_which_option_was_wrong(self):
        # The whole reason the replacement is wanted. mpv printed
        # "Error parsing option definitely-not-an-mpv-option (option not
        # found)" to a stderr nobody captured, and this is what survives.
        with self.assertRaises(python_mpv_jsonipc.MPVError) as caught:
            self.start_with_bad_option()
        self.assertNotIn("definitely", str(caught.exception))

    def test_every_retry_is_spent_on_a_start_that_cannot_succeed(self):
        with mock.patch.object(python_mpv_jsonipc.subprocess, "Popen",
                               wraps=subprocess.Popen) as popen:
            with self.assertRaises(python_mpv_jsonipc.MPVError):
                self.start_with_bad_option(start_retries=3)
            self.assertEqual(popen.call_count, 3)

    def test_the_configured_delay_is_paid_after_every_failure(self):
        # Three failures, three sleeps -- including one after the final
        # attempt, whose only effect is to delay the exception.
        started = time.perf_counter()
        with self.assertRaises(python_mpv_jsonipc.MPVError):
            self.start_with_bad_option(start_retries=3,
                                       start_retry_delay_ms=300)
        elapsed = time.perf_counter() - started
        self.assertGreaterEqual(elapsed, 0.9)


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
        # endpoint is not there, the transport raises straight out of the
        # constructor rather than waiting.
        with self.assertRaises(OSError):
            python_mpv_jsonipc.MPV(start_mpv=False,
                                   ipc_socket="/tmp/definitely-no-mpv-here")


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
