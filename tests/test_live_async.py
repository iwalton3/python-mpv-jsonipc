"""``command_async`` against a real mpv.

The case it exists for is a command that takes a long time and must not hold
the caller or the pipe while it does: jellyfin-mpv-shim's ``sub-add`` of a
subtitle the server extracts before it answers, which held a synchronous
``command()`` for the full 120s ``TIMEOUT`` and then raised. A local HTTP
server that answers late stands in for that server.

The pipe half is the one a fake could not show: whether mpv answers *other*
requests while one is running is mpv's behaviour, and it only does so for a
request that carries ``"async": true``.
"""

import http.server
import threading
import time
import unittest
from unittest import mock

from _harness import LiveMPVTest, python_mpv_jsonipc

SRT = b"1\n00:00:00,000 --> 00:10:00,000\nhello\n"


class _LateServer:
    """``/<seconds>/ok.srt`` answers with a subtitle after that long;
    ``/<seconds>/missing.srt`` with a 404."""

    def __init__(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(handler):
                _, delay, name = handler.path.split("/")
                time.sleep(float(delay))
                if name == "ok.srt":
                    handler.send_response(200)
                    handler.send_header("Content-Length", str(len(SRT)))
                    handler.end_headers()
                    handler.wfile.write(SRT)
                else:
                    handler.send_response(404)
                    handler.send_header("Content-Length", "0")
                    handler.end_headers()

            def log_message(handler, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, delay, name="ok.srt"):
        return "http://127.0.0.1:{0}/{1}/{2}".format(
            self.server.server_address[1], delay, name)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _wait(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class _AsyncTest(LiveMPVTest):
    def setUp(self):
        super().setUp()
        self.http = _LateServer()
        self.addCleanup(self.http.close)
        self.mpv.play("av://lavfi:testsrc")
        self.assertTrue(_wait(lambda: self.mpv.time_pos is not None),
                        "mpv never started playing")

    def external_subs(self):
        return [t for t in self.mpv.track_list or []
                if t.get("type") == "sub" and t.get("external")]


class CommandAsyncTest(_AsyncTest):
    def test_it_returns_before_the_command_finishes(self):
        started = time.monotonic()
        future = self.mpv.command_async("sub-add", self.http.url(2), "select")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(future.done())

        self.assertIsNone(future.result(timeout=15))
        self.assertGreaterEqual(time.monotonic() - started, 2)
        subs = self.external_subs()
        self.assertEqual(len(subs), 1, subs)
        self.assertTrue(subs[0].get("selected"), subs)

    def test_other_requests_are_answered_while_it_runs(self):
        # The difference "async" makes on the wire: without it mpv's IPC
        # handler executes the command inline and reads nothing else from
        # this client until it finishes, so this read would wait out the
        # server's delay.
        future = self.mpv.command_async("sub-add", self.http.url(5))
        started = time.monotonic()
        self.assertIsNotNone(self.mpv.pause)
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(future.done())
        future.result(timeout=15)

    def test_a_failure_arrives_as_the_future_exception(self):
        future = self.mpv.command_async(
            "sub-add", self.http.url(0, "missing.srt"))
        self.assertIsInstance(future.exception(timeout=15),
                              python_mpv_jsonipc.MPVError)

    def test_an_unavailable_property_is_none_as_it_is_for_command(self):
        # testsrc has no audio, so mpv answers "property unavailable", which
        # command() turns into None rather than raising.
        self.assertIsNone(self.mpv.command("get_property", "audio-params"))
        future = self.mpv.command_async("get_property", "audio-params")
        self.assertIsNone(future.result(timeout=5))

    def test_the_callback_sees_the_outcome_and_supplies_the_result(self):
        seen = []

        def callback(error, result):
            seen.append((error, result, threading.current_thread()))
            return "from the callback"

        ok = self.mpv.command_async("get_property", "pause", callback=callback)
        self.assertEqual(ok.result(timeout=5), "from the callback")
        bad = self.mpv.command_async(
            "sub-add", self.http.url(0, "missing.srt"), callback=callback)
        self.assertEqual(bad.result(timeout=15), "from the callback")

        (error, result, thread), (error2, result2, _) = seen
        self.assertIsNone(error)
        self.assertIs(result, False)
        self.assertIsInstance(error2, python_mpv_jsonipc.MPVError)
        self.assertIsNone(result2)
        # Never the transport's reader: user code there stalls every reply.
        self.assertIs(thread, self.mpv.event_handler)

    def test_a_callback_that_raises_becomes_the_future_exception(self):
        def callback(error, result):
            raise ValueError("boom")

        with self.assertLogs("mpv-jsonipc", "ERROR"):
            future = self.mpv.command_async("get_property", "pause",
                                            callback=callback)
            self.assertIsInstance(future.exception(timeout=5), ValueError)

    def test_it_cannot_be_cancelled(self):
        # The IPC protocol has no way to abort a running command.
        future = self.mpv.command_async("sub-add", self.http.url(2))
        self.assertFalse(future.cancel())
        future.result(timeout=15)

    def test_loading_another_file_aborts_a_pending_sub_add(self):
        # mpv's behaviour, pinned because the shim relies on it: a subtitle
        # still loading when the next item starts must not land on that item.
        future = self.mpv.command_async("sub-add", self.http.url(5), "select")
        time.sleep(0.5)
        self.mpv.play("av://lavfi:testsrc")
        self.assertIsInstance(future.exception(timeout=15),
                              python_mpv_jsonipc.MPVError)
        self.assertTrue(_wait(lambda: self.mpv.time_pos is not None))
        self.assertEqual(self.external_subs(), [])


class TeardownTest(_AsyncTest):
    """Every future resolves. One left pending forever is a caller blocked
    on ``result()`` forever, which is worse than the timeout this replaces."""

    def test_terminate_fails_what_is_pending(self):
        future = self.mpv.command_async("sub-add", self.http.url(30))
        self.mpv.terminate()
        self.assertIsInstance(future.exception(timeout=5), BrokenPipeError)

    def test_mpv_dying_fails_what_is_pending(self):
        future = self.mpv.command_async("sub-add", self.http.url(30))
        self.mpv.mpv_process.process.kill()
        self.assertIsInstance(future.exception(timeout=5), BrokenPipeError)

    def test_after_terminate_it_fails_instead_of_hanging(self):
        self.mpv.terminate()
        future = self.mpv.command_async("get_property", "pause")
        self.assertIsInstance(future.exception(timeout=5), BrokenPipeError)

    def test_the_callback_still_runs_after_terminate(self):
        seen = []
        self.mpv.terminate()
        self.mpv.command_async("get_property", "pause",
                               callback=lambda e, r: seen.append(e))
        self.assertTrue(_wait(lambda: seen, 5))
        self.assertIsInstance(seen[0], BrokenPipeError)

    def test_a_callback_can_terminate(self):
        # The callback runs on the event thread, and terminate() stops that
        # thread: it must not try to join itself.
        future = self.mpv.command_async(
            "get_property", "pause",
            callback=lambda e, r: self.mpv.terminate())
        self.assertIsNone(future.exception(timeout=10))
        self.assertTrue(_wait(lambda: self.mpv.mpv_process.process.poll()
                              is not None, 10), "mpv is still running")


class ReplyRoutingTest(unittest.TestCase):
    """The reader thread's side, without a pipe. An exception here escapes
    the transport's read loop, which reports it as the connection dying."""

    def setUp(self):
        transport = mock.MagicMock()
        patch = mock.patch.object(python_mpv_jsonipc, "UnixSocket", transport)
        patch.start()
        self.addCleanup(patch.stop)
        mock.patch.object(python_mpv_jsonipc, "WindowsSocket",
                          transport).start()
        self.addCleanup(mock.patch.stopall)
        self.inter = python_mpv_jsonipc.MPVInter("unused")

    def test_a_reply_nobody_is_waiting_for_is_dropped(self):
        # mpv answers a request that carried no request_id with id 0.
        self.inter.event_callback({"request_id": 0, "error": "success"})
        self.inter.event_callback({"request_id": 12345, "error": "success"})

    def test_an_async_reply_is_delivered_once(self):
        seen = []
        self.inter.command_async(lambda e, r: seen.append((e, r)),
                                 "get_property", "pause")
        sent = self.inter.socket.send.call_args[0][0]
        self.assertIs(sent["async"], True)
        reply = {"request_id": sent["request_id"], "error": "success",
                 "data": True}
        self.inter.event_callback(reply)
        self.inter.event_callback(reply)
        self.inter.stop()
        self.assertEqual(seen, [(None, True)])

    def test_a_send_that_fails_is_reported_to_the_callback(self):
        seen = []
        self.inter.socket.send.side_effect = BrokenPipeError("gone")
        self.inter.command_async(lambda e, r: seen.append(e), "stop")
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], BrokenPipeError)


if __name__ == "__main__":
    unittest.main()
