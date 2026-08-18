"""How Python kwargs become mpv command-line arguments.

Characterization, not aspiration: these assert what the library does today,
including the parts that look accidental. The argv is the whole contract
between this library and mpv, and downstream code passes options through it
that neither this repo nor its author has ever seen -- so the mapping is
pinned exactly, order included.

No mpv runs here. ``Popen`` is captured and the endpoint poll is answered
``True``, which is honest for a test that only ever reads back the argv; see
``_harness`` for why nothing about lifecycle is tested this way.
"""

import os
import unittest
from unittest import mock

from _harness import python_mpv_jsonipc


class _FakeProcess:
    returncode = None

    def poll(self):
        return None

    def terminate(self):
        pass


def build_argv(ipc_socket="/tmp/testsocket", mpv_location="mpv", **kwargs):
    """The argv ``MPVProcess`` would have spawned, without spawning it."""
    with mock.patch.object(python_mpv_jsonipc.subprocess, "Popen") as popen, \
            mock.patch.object(python_mpv_jsonipc, "_ipc_endpoint_ready",
                              return_value=True):
        popen.return_value = _FakeProcess()
        python_mpv_jsonipc.MPVProcess(ipc_socket, mpv_location, **kwargs)
        return popen.call_args[0][0]


class ArgumentTranslationTest(unittest.TestCase):
    def test_the_binary_leads_and_defaults_follow_the_callers_options(self):
        # Order is observable and therefore pinned. `_set_default` appends,
        # so anything the caller did not pass arrives after what they did,
        # in the order the defaults are applied.
        self.assertEqual(build_argv(mpv_location="/usr/bin/mpv"), [
            "/usr/bin/mpv",
            "--idle=yes",
            "--input-ipc-server=/tmp/testsocket",
            "--input-terminal=no",
            "--terminal=no",
        ])

    def test_underscores_become_dashes(self):
        self.assertIn("--hwdec-codecs=all", build_argv(hwdec_codecs="all"))

    def test_booleans_become_yes_and_no(self):
        argv = build_argv(fullscreen=True, border=False)
        self.assertIn("--fullscreen=yes", argv)
        self.assertIn("--border=no", argv)

    def test_numbers_are_not_booleans(self):
        # 1 == True and 0 == False in Python, so an `==` comparison turns
        # --video-scale-x=1 into --video-scale-x=yes, which mpv rejects.
        # Fixed in 8ddcfec; pinned here because the bug is invisible until
        # mpv refuses the option.
        argv = build_argv(video_scale_x=1, volume=0)
        self.assertIn("--video-scale-x=1", argv)
        self.assertIn("--volume=0", argv)

    def test_floats_and_strings_pass_through_as_written(self):
        argv = build_argv(speed=1.5, title="a movie")
        self.assertIn("--speed=1.5", argv)
        self.assertIn("--title=a movie", argv)

    def test_a_list_becomes_a_repeated_flag(self):
        argv = build_argv(script=["a.lua", "b.lua"])
        self.assertEqual([a for a in argv if a.startswith("--script=")],
                         ["--script=a.lua", "--script=b.lua"])

    def test_the_caller_can_override_every_default(self):
        argv = build_argv(idle=False, terminal=True, input_terminal=True)
        self.assertIn("--idle=no", argv)
        self.assertIn("--terminal=yes", argv)
        self.assertIn("--input-terminal=yes", argv)
        self.assertEqual(len([a for a in argv if a.startswith("--idle=")]), 1)

    def test_the_ipc_server_option_is_not_overridable_by_that_name(self):
        # input_ipc_server is a default like any other, so a caller CAN
        # replace it -- and then the library connects to the socket it was
        # told about rather than the one mpv opens. Pinned as-is: it is a
        # foot-gun, but changing it would change behaviour.
        argv = build_argv(input_ipc_server="/tmp/elsewhere")
        self.assertIn("--input-ipc-server=/tmp/elsewhere", argv)
        self.assertNotIn("--input-ipc-server=/tmp/testsocket", argv)


class SocketPathTest(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX socket paths only")
    def test_a_stale_socket_file_is_removed_before_starting(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "stale.sock")
        with open(path, "w") as handle:
            handle.write("")
        self.assertTrue(os.path.exists(path))
        build_argv(ipc_socket=path)
        self.assertFalse(os.path.exists(path))


class MpvFmtTest(unittest.TestCase):
    """`_mpv_fmt` in isolation, since it is where the bool/int trap lives."""

    def setUp(self):
        self.fmt = python_mpv_jsonipc.MPVProcess._mpv_fmt

    def test_identity_comparison_keeps_numbers_intact(self):
        self.assertEqual(self.fmt(None, True), "yes")
        self.assertEqual(self.fmt(None, False), "no")
        self.assertEqual(self.fmt(None, 1), 1)
        self.assertEqual(self.fmt(None, 0), 0)
        self.assertEqual(self.fmt(None, "yes"), "yes")


if __name__ == "__main__":
    unittest.main()
