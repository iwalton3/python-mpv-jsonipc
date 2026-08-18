"""The module must stay parseable by the Python it claims to support.

`pyproject.toml` says `requires-python = ">=3.6"`, and pip believes it: a
stray f-string with `=`, a walrus, or `match` would install cleanly on an old
interpreter and then fail at import, on a machine no CI here can reach.

Raising the floor is itself a downstream break -- pip refuses to install on
anything below it -- so the floor is treated as fixed and the syntax is what
gets checked. The test reads the version from `pyproject.toml` rather than
hard-coding it, so lowering or raising the claim moves the check with it.

Only the shipped module is checked. The tests themselves are free to use
whatever the CI interpreters provide.
"""

import ast
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE = os.path.join(ROOT, "python_mpv_jsonipc.py")
PYPROJECT = os.path.join(ROOT, "pyproject.toml")


def declared_floor():
    with open(PYPROJECT) as handle:
        match = re.search(r'requires-python\s*=\s*"[><=~^]*\s*(\d+)\.(\d+)',
                          handle.read())
    if not match:
        raise AssertionError("no requires-python in pyproject.toml")
    return int(match.group(1)), int(match.group(2))


class PythonFloorTest(unittest.TestCase):
    def test_the_module_parses_as_the_oldest_supported_python(self):
        floor = declared_floor()
        with open(MODULE) as handle:
            source = handle.read()
        try:
            ast.parse(source, filename=MODULE, feature_version=floor)
        except SyntaxError as error:
            self.fail(
                "python_mpv_jsonipc.py uses syntax newer than the declared "
                "floor {0}.{1}: {2} (line {3}). Either rewrite it or change "
                "requires-python -- but note that raising the floor stops "
                "pip installing for everyone below it.".format(
                    floor[0], floor[1], error.msg, error.lineno))

    def test_the_floor_check_can_actually_fail(self):
        # The check above passes trivially if `feature_version` is ignored,
        # which is the shape of a test that cannot fail. Prove the mechanism
        # rejects something the floor genuinely lacks (walrus is 3.8).
        floor = declared_floor()
        if floor >= (3, 8):
            self.skipTest("floor is 3.8+, walrus is valid there")
        with self.assertRaises(SyntaxError):
            ast.parse("if (n := 1): pass", feature_version=floor)


if __name__ == "__main__":
    unittest.main()
