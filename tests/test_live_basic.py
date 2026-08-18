"""Properties and commands against a real mpv.

Everything here needs a live mpv on purpose. The dynamic attribute machinery
is built from what mpv answers at construction time -- `property-list` and
`command-list` -- so a stand-in that returns a convenient list would be
testing the list, not the library.
"""

import unittest

import os

from _harness import (LiveMPVTest, MPV_BINARY,
                      python_mpv_jsonipc)


class ConstructionTest(LiveMPVTest):
    def test_properties_are_discovered_and_underscored(self):
        self.assertIn("pause", self.mpv.properties)
        self.assertIn("playback_time", self.mpv.properties)   # playback-time
        self.assertNotIn("playback-time", self.mpv.properties)

    def test_commands_are_attached_as_methods(self):
        for command in ("loadfile", "seek", "quit", "keybind"):
            self.assertTrue(callable(getattr(self.mpv, command, None)),
                            "{0} is not callable".format(command))

    def test_json_ipc_only_commands_are_not_attached(self):
        # These are not in mpv's `command-list` -- they exist only over the
        # IPC transport -- so they never become methods. Callers reach them
        # through `command()`, or through the wrappers that exist for the
        # two that have one (`bind_property_observer`, `wait_for_property`).
        # Worth pinning: it looks like an omission and is the actual design.
        for command in ("observe_property", "unobserve_property",
                        "get_property", "set_property",
                        "request_log_messages", "client_name"):
            self.assertFalse(callable(getattr(self.mpv, command, None)),
                             "{0} unexpectedly became a method".format(command))

    def test_a_command_that_collides_with_a_property_gets_a_cmd_suffix(self):
        # `af` and `vf` are both, on every mpv this has been run against.
        # Discovered rather than hard-coded so a change in mpv shows up as a
        # skip, not a spurious failure.
        collisions = [name for name in ("af", "vf")
                      if name in self.mpv.properties]
        if not collisions:
            self.skipTest("this mpv has no command/property name collisions")
        for name in collisions:
            self.assertTrue(callable(getattr(self.mpv, name + "_cmd", None)))

    def test_dir_lists_properties_alongside_real_attributes(self):
        listing = dir(self.mpv)
        self.assertIn("pause", listing)
        self.assertIn("terminate", listing)


class PropertyAccessTest(LiveMPVTest):
    def test_a_property_round_trips_through_attribute_access(self):
        self.mpv.pause = True
        self.assertIs(self.mpv.pause, True)
        self.mpv.pause = False
        self.assertIs(self.mpv.pause, False)

    def test_an_unavailable_property_reads_as_none_rather_than_raising(self):
        # Nothing is loaded, so mpv answers "property unavailable". The
        # library turns exactly that one error into None; downstream code
        # leans on it heavily (`if mpv.width:`).
        self.assertIsNone(self.mpv.width)

    def test_an_unknown_property_raises_mpv_error(self):
        with self.assertRaises(python_mpv_jsonipc.MPVError) as caught:
            self.mpv.command("get_property", "no-such-property")
        self.assertEqual(str(caught.exception), "property not found")

    def test_an_unknown_attribute_is_a_normal_attribute_error(self):
        # Not an MPVError: `properties` gates the property path, so anything
        # else falls through to ordinary attribute lookup.
        with self.assertRaises(AttributeError):
            self.mpv.definitely_not_a_property

    def test_setting_a_non_property_attribute_stays_local(self):
        self.mpv.my_own_bookkeeping = 42
        self.assertEqual(self.mpv.my_own_bookkeeping, 42)


class CommandTest(LiveMPVTest):
    def test_command_returns_the_data_field(self):
        self.assertIsInstance(self.mpv.command("get_property", "mpv-version"),
                              str)

    def test_play_is_an_alias_for_loadfile(self):
        seen = []
        self.mpv.loadfile = lambda *args: seen.append(args)
        self.mpv.play("av://lavfi:testsrc")
        self.assertEqual(seen, [("av://lavfi:testsrc",)])


class AttachToExistingMPVTest(unittest.TestCase):
    """`start_mpv=False` -- attaching to an mpv somebody else started.

    jellyfin-mpv-shim's `mpv_ext_start: false` runs this way, and it is the
    path with the least coverage elsewhere: no `MPVProcess` is built, so
    `_ipc_endpoint_ready` never runs and the transport's own connect retry is
    the only thing that waits for the endpoint.
    """

    @classmethod
    def setUpClass(cls):
        if MPV_BINARY is None:
            raise unittest.SkipTest("no mpv binary found")

    def setUp(self):
        import random
        import subprocess
        import time

        name = "mpvtest{0}".format(random.randint(0, 2 ** 48))
        # Windows wants a bare pipe name (the library prepends the
        # \\.\pipe\ prefix); POSIX wants a filesystem path.
        self.ipc_socket = name if os.name == "nt" else "/tmp/" + name

        self.process = subprocess.Popen([
            MPV_BINARY, "--idle=yes", "--config=no", "--vo=null", "--ao=null",
            "--terminal=no", "--input-ipc-server=" + self.ipc_socket])
        self.addCleanup(self._stop_mpv)

        deadline = time.time() + 10
        while time.time() < deadline:
            if python_mpv_jsonipc._ipc_endpoint_ready(
                    self.ipc_socket if os.name != "nt"
                    else "\\\\.\\pipe\\" + self.ipc_socket):
                break
            time.sleep(0.1)
        else:
            self.fail("mpv never opened its IPC endpoint")

    def _stop_mpv(self):
        self.process.terminate()
        self.process.wait(timeout=10)
        if os.name != "nt" and os.path.exists(self.ipc_socket):
            os.remove(self.ipc_socket)

    def test_it_attaches_and_talks_to_the_running_mpv(self):
        mpv = python_mpv_jsonipc.MPV(start_mpv=False,
                                     ipc_socket=self.ipc_socket)
        self.addCleanup(mpv.terminate)
        self.assertIsInstance(mpv.command("get_property", "mpv-version"), str)
        mpv.pause = True
        self.assertIs(mpv.pause, True)

    def test_terminate_leaves_the_process_it_did_not_start_alone(self):
        # It never owned the process, so it must not kill it. A host that
        # attaches to the user's own mpv would otherwise shut it down.
        mpv = python_mpv_jsonipc.MPV(start_mpv=False,
                                     ipc_socket=self.ipc_socket)
        self.assertIsNone(mpv.mpv_process)
        mpv.terminate()
        self.assertIsNone(self.process.poll(),
                          "terminate() killed an mpv it did not start")


class TeardownTest(LiveMPVTest):
    def test_terminate_is_safe_to_call_twice(self):
        # __del__ calls terminate() too, so every user of this library
        # terminates at least twice whether they meant to or not.
        self.mpv.terminate()
        self.mpv.terminate()

    @unittest.skipIf(__import__("os").name == "nt", "POSIX socket files only")
    def test_terminate_removes_the_socket_file_it_created(self):
        # A long-running host that opens and closes many mpv instances would
        # otherwise fill /tmp with dead sockets.
        import glob
        import os
        socket_path = self.mpv.mpv_process.ipc_socket
        self.assertTrue(os.path.exists(socket_path))
        self.mpv.terminate()
        self.assertFalse(os.path.exists(socket_path))

    def test_commands_after_terminate_fail_fast_rather_than_hanging(self):
        # The 120s command timeout would be a very long way to discover a
        # closed socket. The transport raises instead.
        self.mpv.terminate()
        with self.assertRaises(BrokenPipeError):
            self.mpv.command("get_property", "pause")


if __name__ == "__main__":
    unittest.main()
