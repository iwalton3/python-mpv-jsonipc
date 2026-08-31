"""A minimal client, for running as a PyInstaller-frozen binary.

Not part of the unittest suite -- it is the payload for the `windows-frozen`
CI job. What it proves is narrow and not covered anywhere else: that the
Windows named-pipe transport still works when the interpreter is frozen. That
path is now `ctypes` against `kernel32`, which is what the transport was
rewritten onto precisely because it survives freezing without hooks or
bundled DLLs -- but "no hook needed" is a claim about PyInstaller, not a
guarantee, and only a frozen binary opening a real pipe can check it.

Exits non-zero with a message on any failure, so the CI step is the assertion.
"""

import os
import sys

if not getattr(sys, "frozen", False):
    # Running from a checkout. Frozen, the module is bundled by PyInstaller
    # from the *installed* package -- its analysis is static and cannot
    # follow a sys.path insert, so a checkout-relative import would produce
    # a binary with no library in it at all. (It did: "No module named
    # python_mpv_jsonipc", the first time this job ran.) CI pip-installs
    # before freezing, which is also what downstream projects do.
    sys.path.insert(0,
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
