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


#: Syntax that does not exist before the stated version, for probing whether
#: this interpreter enforces `feature_version` at all.
PROBES = (
    ((3, 8), "if (n := 1): pass"),
    ((3, 10), "match 1:\n    case 1: pass\n"),
)


def enforcement_probe(floor):
    """A snippet newer than *floor*, or None if we have nothing newer."""
    for version, source in PROBES:
        if version > floor:
            return source
    return None


def enforces_feature_version(floor):
    """Whether `ast.parse` on THIS interpreter rejects newer syntax.

    Not a version comparison: CPython's enforcement has been tightened over
    time, and 3.9 accepts a walrus at `feature_version=(3, 6)` where 3.13
    rejects it. Asking the interpreter is the only reliable form of the
    question -- discovered when this file's own meta-test failed on the 3.9
    CI leg and nowhere else.
    """
    source = enforcement_probe(floor)
    if source is None:
        return False
    try:
        ast.parse(source, feature_version=floor)
    except SyntaxError:
        return True
    return False


class PythonFloorTest(unittest.TestCase):
    def test_the_module_parses_as_the_oldest_supported_python(self):
        floor = declared_floor()
        if not enforces_feature_version(floor):
            # Honest skip rather than a pass that proves nothing. The guard
            # only has teeth on interpreters that enforce feature_version,
            # and the CI matrix includes several that do.
            self.skipTest(
                "this interpreter does not enforce feature_version={0}; "
                "the check is meaningless here".format(floor))
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

    def test_at_least_the_mechanism_is_exercised_somewhere(self):
        # The check above passes trivially if `feature_version` is ignored,
        # which is the shape of a test that cannot fail. This asserts the
        # rejection really happens wherever it is claimed to.
        floor = declared_floor()
        if not enforces_feature_version(floor):
            self.skipTest("no enforcement on this interpreter")
        with self.assertRaises(SyntaxError):
            ast.parse(enforcement_probe(floor), feature_version=floor)


if __name__ == "__main__":
    unittest.main()
