"""A minimal client, for running as a PyInstaller-frozen binary.

Not part of the unittest suite -- it is the payload for the `windows-frozen`
CI job. What it proves is narrow and not covered anywhere else: that the
Windows named-pipe transport still works when the interpreter is frozen. That
path reaches `_winapi` and `multiprocessing.connection.PipeConnection`, and a
freezer that fails to bundle either produces a binary that imports fine and
dies the moment it opens a pipe.

Exits non-zero with a message on any failure, so the CI step is the assertion.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import python_mpv_jsonipc  # noqa: E402


def main():
    binary = os.environ.get("MPV_BINARY")
    mpv = python_mpv_jsonipc.MPV(mpv_location=binary, idle=True, config=False,
                                 vo="null", ao="null")
    try:
        version = mpv.command("get_property", "mpv-version")
        if not version:
            raise AssertionError("mpv answered no version over the pipe")
        mpv.pause = True
        if mpv.pause is not True:
            raise AssertionError("property round trip failed over the pipe")
        print("frozen client reached {0} and round-tripped a property".format(
            version))
    finally:
        mpv.terminate()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - the CI step is the assertion
        print("FAILED: {0}: {1}".format(type(error).__name__, error))
        raise SystemExit(1)
