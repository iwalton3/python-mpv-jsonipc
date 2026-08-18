"""The public surface, frozen.

Roughly 78 repositories depend on this library, some unmaintained, and a name
or keyword argument that quietly changes shape breaks them at *their* users'
machines rather than here. Keyword names matter as much as the names
themselves: callers pass `mpv_location=`, `quit_callback=`, `start_retries=`
by keyword, so a rename is a break even when the parameter still exists.

This is a snapshot, not a judgement. Nothing here says the surface is good --
only that it is what shipped. Changing it deliberately means regenerating the
golden file in the same commit, which is what makes the change visible in
review instead of six months later in someone's issue tracker.

    python3 tests/test_api_surface.py --update
"""

import inspect
import json
import os
import sys
import unittest

from _harness import python_mpv_jsonipc

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "api_surface.json")


def _signature(func):
    try:
        return str(inspect.signature(func))
    except (TypeError, ValueError):  # pragma: no cover - C callables
        return "(?)"


def describe():
    """The public surface as plain data, stable under dict ordering."""
    module = python_mpv_jsonipc
    surface = {"classes": {}, "functions": {}, "constants": {}}

    for name in sorted(dir(module)):
        if name.startswith("_"):
            continue
        value = getattr(module, name)
        if inspect.isclass(value):
            if value.__module__ != module.__name__:
                continue  # imported, not ours
            methods = {}
            for attr in sorted(dir(value)):
                if attr.startswith("_") and attr != "__init__":
                    continue
                member = inspect.getattr_static(value, attr, None)
                if callable(member):
                    methods[attr] = _signature(member)
            surface["classes"][name] = {
                "bases": [b.__name__ for b in value.__bases__],
                "methods": methods,
            }
        elif inspect.isfunction(value):
            surface["functions"][name] = _signature(value)
        elif isinstance(value, (int, float, str, bool, list, tuple)):
            surface["constants"][name] = value

    return surface


class ApiSurfaceTest(unittest.TestCase):
    def test_the_public_surface_matches_the_golden_file(self):
        with open(GOLDEN) as handle:
            expected = json.load(handle)
        actual = describe()
        self.assertEqual(
            expected, actual,
            "The public API changed. If that was deliberate, regenerate the "
            "golden file with `python3 tests/test_api_surface.py --update` "
            "in the same commit -- and consider whether downstream users who "
            "never read the changelog will survive it.")

    def test_the_names_downstream_actually_imports_are_present(self):
        # Named individually as well as by snapshot, so a wholesale
        # regeneration of the golden file cannot silently bless a removal.
        for name in ("MPV", "MPVError", "MPVProcess", "MPVInter",
                     "EventHandler", "UnixSocket"):
            self.assertTrue(hasattr(python_mpv_jsonipc, name),
                            "{0} disappeared from the public surface".format(name))

    def test_mpv_error_stays_an_exception(self):
        self.assertTrue(issubclass(python_mpv_jsonipc.MPVError, Exception))
        # Downstream catches it and reads str(); both are contract.
        self.assertEqual(str(python_mpv_jsonipc.MPVError("boom")), "boom")

    def test_the_constructor_keywords_keep_their_names_and_defaults(self):
        parameters = inspect.signature(python_mpv_jsonipc.MPV.__init__).parameters
        self.assertEqual(parameters["start_mpv"].default, True)
        self.assertEqual(parameters["ipc_socket"].default, None)
        self.assertEqual(parameters["mpv_location"].default, None)
        self.assertEqual(parameters["log_handler"].default, None)
        self.assertEqual(parameters["loglevel"].default, None)
        self.assertEqual(parameters["quit_callback"].default, None)
        self.assertEqual(parameters["start_retries"].default, 5)
        self.assertEqual(parameters["start_retry_delay_ms"].default, 1000)
        self.assertEqual(parameters["kwargs"].kind,
                         inspect.Parameter.VAR_KEYWORD)


class FallbackCommandListTest(unittest.TestCase):
    """Only reachable on very old mpv, and therefore never exercised by the
    live tests -- which is exactly why it needs pinning here."""

    def test_it_still_contains_what_a_client_needs_to_play_something(self):
        commands = python_mpv_jsonipc.FALLBACK_COMMAND_LIST
        for essential in ("loadfile", "quit", "seek", "stop", "set",
                          "keybind", "script-message"):
            self.assertIn(essential, commands)

    def test_it_is_a_list_of_plain_strings(self):
        self.assertIsInstance(python_mpv_jsonipc.FALLBACK_COMMAND_LIST, list)
        for command in python_mpv_jsonipc.FALLBACK_COMMAND_LIST:
            self.assertIsInstance(command, str)


if __name__ == "__main__":
    if "--update" in sys.argv:
        with open(GOLDEN, "w") as handle:
            json.dump(describe(), handle, indent=2, sort_keys=True)
            handle.write("\n")
        print("wrote {0}".format(GOLDEN))
        sys.exit(0)
    unittest.main()
