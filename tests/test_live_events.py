"""Observers, events, key bindings and log forwarding, against a real mpv.

Every callback here is delivered on the `EventHandler` thread, so each test
waits on a `threading.Event` with a bounded timeout rather than sleeping. A
test that sleeps a fixed amount either wastes the time or fails on a loaded
CI runner, and usually both.
"""

import threading
import unittest

from _harness import (LIVE_OPTIONS, LiveMPVTest, MPV_BINARY,
                      python_mpv_jsonipc)

TIMEOUT = 10


def waiter():
    """An event plus a recorder, for asserting on an async callback."""
    seen = []
    fired = threading.Event()

    def record(*args):
        seen.append(args)
        fired.set()

    return seen, fired, record


class PropertyObserverTest(LiveMPVTest):
    def test_observer_ids_start_at_one_and_increment(self):
        first = self.mpv.bind_property_observer("pause", lambda *_: None)
        second = self.mpv.bind_property_observer("volume", lambda *_: None)
        self.assertEqual(first, 1)
        self.assertEqual(second, 2)

    def test_an_observer_is_called_with_the_name_and_value(self):
        seen, fired, record = waiter()
        self.mpv.bind_property_observer("pause", record)
        # mpv sends the current value immediately on observe, before any
        # change -- that first notification is what `wait_for_property`
        # exists to skip.
        self.assertTrue(fired.wait(TIMEOUT), "observer never fired")
        self.assertEqual(seen[0][0], "pause")

    def test_an_observer_sees_a_change(self):
        seen, fired, record = waiter()
        self.mpv.bind_property_observer("pause", record)
        self.assertTrue(fired.wait(TIMEOUT))
        fired.clear()
        self.mpv.pause = True
        self.assertTrue(fired.wait(TIMEOUT), "no notification for the change")
        self.assertEqual(seen[-1], ("pause", True))

    def test_unbinding_stops_the_callbacks_and_forgets_the_binding(self):
        observer_id = self.mpv.bind_property_observer("pause", lambda *_: None)
        self.assertIn(observer_id, self.mpv.property_bindings)
        self.mpv.unbind_property_observer(observer_id)
        self.assertNotIn(observer_id, self.mpv.property_bindings)

    def test_property_observer_decorator_binds(self):
        seen, fired, record = waiter()

        @self.mpv.property_observer("pause")
        def _handler(name, value):
            record(name, value)

        self.assertTrue(fired.wait(TIMEOUT))


class WaitForPropertyTest(LiveMPVTest):
    def test_it_returns_only_after_an_actual_change(self):
        # The first notification is mpv reporting the current value. If
        # wait_for_property honoured it, this would return before `pause`
        # was ever set and the assertion below would be a coin flip.
        done = threading.Event()

        def wait():
            self.mpv.wait_for_property("pause")
            done.set()

        thread = threading.Thread(target=wait, daemon=True)
        thread.start()
        self.assertFalse(done.wait(1.0),
                         "returned on the initial value notification")
        self.mpv.pause = True
        self.assertTrue(done.wait(TIMEOUT), "never returned after the change")


class EventTest(LiveMPVTest):
    def test_on_event_receives_mpv_events(self):
        seen, fired, record = waiter()
        self.mpv.on_event("client-message")(record)
        self.mpv.command("script-message", "hello")
        self.assertTrue(fired.wait(TIMEOUT), "no client-message event")
        self.assertEqual(seen[-1][0]["args"], ["hello"])

    def test_event_callback_is_an_alias_of_on_event(self):
        # Kept for python-mpv compatibility; downstream uses both spellings.
        seen, fired, record = waiter()
        self.mpv.event_callback("client-message")(record)
        self.mpv.command("script-message", "via-alias")
        self.assertTrue(fired.wait(TIMEOUT))

    def test_bind_event_allows_several_callbacks_for_one_event(self):
        first, fired_first, record_first = waiter()
        second, fired_second, record_second = waiter()
        self.mpv.bind_event("client-message", record_first)
        self.mpv.bind_event("client-message", record_second)
        self.mpv.command("script-message", "both")
        self.assertTrue(fired_first.wait(TIMEOUT))
        self.assertTrue(fired_second.wait(TIMEOUT))


class KeyBindingTest(LiveMPVTest):
    def test_a_bound_key_fires_its_callback(self):
        # Round trips through mpv: keybind -> script-message custom-bind
        # <id> -> client-message -> the callback registry.
        seen, fired, record = waiter()
        self.mpv.bind_key_press("a", record)
        self.mpv.command("keypress", "a")
        self.assertTrue(fired.wait(TIMEOUT), "key binding never fired")

    def test_on_key_press_decorator_binds(self):
        seen, fired, record = waiter()

        @self.mpv.on_key_press("b")
        def _handler():
            record()

        self.mpv.command("keypress", "b")
        self.assertTrue(fired.wait(TIMEOUT))


class LogHandlerTest(unittest.TestCase):
    """The one existing opt-in output channel. Off unless BOTH log_handler
    and loglevel are given -- pinned because the new startup-failure
    diagnosis should follow this shape rather than invent another."""

    def _mpv(self, **extra):
        options = dict(LIVE_OPTIONS)
        options.update(extra)
        instance = python_mpv_jsonipc.MPV(mpv_location=MPV_BINARY, **options)
        self.addCleanup(instance.terminate)
        return instance

    @classmethod
    def setUpClass(cls):
        if MPV_BINARY is None:
            raise unittest.SkipTest("no mpv binary found")

    def test_a_handler_with_a_level_receives_log_messages(self):
        # `debug`, not `info`: an idle mpv says nothing at info or v once it
        # has finished starting, and `request_log_messages` only forwards
        # what is emitted after the request. Measured -- at info and v this
        # test waits the full timeout and sees zero messages.
        seen, fired, record = waiter()
        mpv = self._mpv(log_handler=record, loglevel="debug")
        mpv.command("script-message", "provoke-some-logging")
        mpv.command("get_property", "mpv-version")
        self.assertTrue(fired.wait(TIMEOUT), "no log messages arrived")
        level, prefix, text = seen[0]
        self.assertIsInstance(level, str)
        self.assertIsInstance(prefix, str)
        self.assertIsInstance(text, str)

    def test_a_handler_without_a_level_is_ignored(self):
        seen, fired, record = waiter()
        mpv = self._mpv(log_handler=record)
        mpv.command("script-message", "provoke-some-logging")
        mpv.command("get_property", "mpv-version")
        self.assertFalse(fired.wait(1.0),
                         "log messages arrived without a loglevel")


if __name__ == "__main__":
    unittest.main()
