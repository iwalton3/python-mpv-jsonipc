"""Shared fixtures for the test suite.

Two kinds of test live in here, and the split is deliberate:

* **Pure tests** patch ``subprocess.Popen`` and ``_ipc_endpoint_ready`` so the
  argv translation can be read back without an mpv anywhere. That is safe
  *only* because they assert on the argv string and nothing else -- a stand-in
  that always answers cannot fail the way a real pipe fails.
* **Live tests** drive a real mpv. Everything about process, socket and pipe
  lifecycle is here, because that is the half a mock is structurally unable to
  model, and it is where this library's real bugs are.

``MPV_BINARY`` overrides the mpv used, for CI images that do not put it on
PATH.
"""

import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import python_mpv_jsonipc  # noqa: E402


def find_mpv():
    """Path to an mpv binary, or None."""
    override = os.environ.get("MPV_BINARY")
    if override:
        return override if os.path.exists(override) else None
    return shutil.which("mpv.exe" if os.name == "nt" else "mpv")


MPV_BINARY = find_mpv()

if MPV_BINARY is None and os.environ.get("REQUIRE_MPV"):
    # CI sets REQUIRE_MPV. Without it, a mistyped MPV_BINARY -- a path that
    # does not exist, an archive that extracted into a subdirectory -- makes
    # every live test SKIP, and a build that skipped the entire live suite
    # reports green while having tested nothing at all. Failing loudly here
    # is the difference between "no mpv on this laptop" and "CI is lying".
    raise RuntimeError(
        "REQUIRE_MPV is set but no mpv was found. MPV_BINARY={0!r}".format(
            os.environ.get("MPV_BINARY")))

requires_mpv = unittest.skipIf(
    MPV_BINARY is None,
    "no mpv binary found (set MPV_BINARY to override)")


#: Options every live test starts mpv with. No window, no audio device, and
#: no config from the machine running the tests -- a user's input.conf or
#: mpv.conf would otherwise change what the library sees.
LIVE_OPTIONS = dict(
    idle=True,
    config=False,
    vo="null",
    ao="null",
)


class LiveMPVTest(unittest.TestCase):
    """A real mpv per test, torn down however the test leaves it."""

    extra_options = {}

    @classmethod
    def setUpClass(cls):
        if MPV_BINARY is None:
            raise unittest.SkipTest("no mpv binary found")

    def setUp(self):
        options = dict(LIVE_OPTIONS)
        options.update(self.extra_options)
        self.mpv = python_mpv_jsonipc.MPV(mpv_location=MPV_BINARY, **options)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            self.mpv.terminate()
        except Exception:
            pass
