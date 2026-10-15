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
import json
import logging
import os
import socket
import sys
import tempfile
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
    """A closed connection resolves every future, and a callback that
    raises, SystemExit included, strands none. One left pending forever is a
    caller blocked on ``result()`` forever, which is worse than the timeout
    this replaces."""

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

    def test_a_callback_that_exits_strands_nothing(self):
        # A BaseException from a callback ends the event thread, as it always
        # has; what was waiting behind it must still resolve, and so must
        # what comes after.
        def leave(error, result):
            sys.exit(3)

        first = self.mpv.command_async("get_property", "pause", callback=leave)
        second = self.mpv.command_async("get_property", "pause")
        self.assertIsInstance(first.exception(timeout=5), SystemExit)
        self.assertIs(second.result(timeout=5), False)
        self.assertTrue(_wait(lambda: not self.mpv.event_handler.is_alive(), 5))
        self.assertIs(self.mpv.command_async("get_property", "pause")
                      .result(timeout=5), False)

    def test_a_callback_that_exits_on_the_reader_ends_nothing(self):
        # Once a callback has ended the event thread, later callbacks run on
        # the reader. One that exits there must not end the read loop too:
        # replies still arrive, and mpv dying is still noticed.
        def leave(error, result):
            ran_on.append(threading.current_thread())
            sys.exit(3)

        ran_on = []
        self.mpv.command_async("get_property", "pause",
                               callback=leave).exception(timeout=5)
        self.assertTrue(_wait(lambda: not self.mpv.event_handler.is_alive(), 5))
        self.assertIsInstance(self.mpv.command_async(
            "get_property", "pause", callback=leave).exception(timeout=5),
            SystemExit)
        self.assertIs(ran_on[1], self.mpv.mpv_inter.socket)
        with mock.patch.object(python_mpv_jsonipc, "TIMEOUT", 3):
            self.assertIs(self.mpv.command("get_property", "pause"), False)
        pending = self.mpv.command_async("sub-add", self.http.url(30))
        self.mpv.mpv_process.process.kill()
        self.assertIsInstance(pending.exception(timeout=5), BrokenPipeError)

    def test_mpv_dying_fails_a_waiting_command_at_once(self):
        # command() used to wait out TIMEOUT (120 s) for a reply that could
        # no longer come, holding whatever lock its caller held.
        outcome = []

        def wait():
            try:
                self.mpv.command("sub-add", self.http.url(30))
            except BaseException as error:
                outcome.append(error)

        waiter = threading.Thread(target=wait, daemon=True)
        waiter.start()
        time.sleep(0.5)
        self.mpv.mpv_process.process.kill()
        waiter.join(10)
        self.assertFalse(waiter.is_alive(), "command() is still waiting")
        self.assertIsInstance(outcome[0], BrokenPipeError)


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
        self.quits = []
        self.inter.quit_callback = lambda: self.quits.append(True)

    def player_whose_event_thread_is_gone(self):
        """An MPV over the stub, in the state a callback's SystemExit leaves
        it: put_task refuses, so callbacks run on whichever thread delivers
        the reply."""
        player = python_mpv_jsonipc.MPV.__new__(python_mpv_jsonipc.MPV)
        handler = python_mpv_jsonipc.EventHandler()
        handler.stop(join=False)
        for name, value in [("properties", set()), ("mpv_process", None),
                            ("mpv_inter", self.inter),
                            ("event_handler", handler)]:
            object.__setattr__(player, name, value)
        return player

    def request_id(self, n):
        return self.inter.socket.send.call_args_list[n][0][0]["request_id"]

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

    def test_a_raising_on_reply_loses_nothing_else_on_close(self):
        seen, quits = [], []
        self.inter.quit_callback = lambda: quits.append(True)

        def bad(error, result):
            raise ValueError("a caller's bug")

        self.inter.command_async(bad, "get_property", "pause")
        self.inter.command_async(lambda e, r: seen.append(e),
                                 "get_property", "pause")
        with self.assertLogs("mpv-jsonipc", "ERROR"):
            self.inter._on_quit()
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], BrokenPipeError)
        self.assertEqual(quits, [True])

    def test_a_raising_on_reply_does_not_end_the_read_loop(self):
        def bad(error, result):
            raise ValueError("a caller's bug")

        self.inter.command_async(bad, "get_property", "pause")
        sent = self.inter.socket.send.call_args[0][0]
        with self.assertLogs("mpv-jsonipc", "ERROR"):
            self.inter.event_callback({"request_id": sent["request_id"],
                                       "error": "success", "data": True})

    def test_an_exiting_callback_on_the_reader_is_logged(self):
        player = self.player_whose_event_thread_is_gone()
        first = player.command_async("get_property", "pause",
                                     callback=lambda e, r: sys.exit(3))
        second = player.command_async("get_property", "pause")
        try:
            with self.assertLogs("mpv-jsonipc", "ERROR"):
                self.inter.event_callback({"request_id": self.request_id(0),
                                           "error": "success", "data": False})
        except SystemExit:
            self.fail("SystemExit escaped event_callback, ending the reader")
        self.assertIsInstance(first.exception(0), SystemExit)
        self.inter._on_quit()
        self.assertIsInstance(second.exception(0), BrokenPipeError)
        self.assertEqual(self.quits, [True])

    def test_an_exiting_callback_does_not_cut_the_close_short(self):
        player = self.player_whose_event_thread_is_gone()
        first = player.command_async("get_property", "pause",
                                     callback=lambda e, r: sys.exit(3))
        rest = [player.command_async("get_property", "pause")
                for _ in range(2)]
        try:
            with self.assertLogs("mpv-jsonipc", "ERROR"):
                self.inter._on_quit()
        except SystemExit:
            self.fail("SystemExit escaped the close sweep")
        self.assertIsInstance(first.exception(0), SystemExit)
        for future in rest:
            self.assertIsInstance(future.exception(0), BrokenPipeError)
        self.assertEqual(self.quits, [True])

    def test_terminate_logs_an_exiting_callback_instead_of_raising_it(self):
        # The callback runs on terminate()'s caller here, but it is the
        # library's close doing the calling: the rest must still resolve.
        player = self.player_whose_event_thread_is_gone()
        first = player.command_async("get_property", "pause",
                                     callback=lambda e, r: sys.exit(3))
        second = player.command_async("get_property", "pause")
        try:
            with self.assertLogs("mpv-jsonipc", "ERROR"):
                player.terminate()
        except SystemExit:
            self.fail("SystemExit raised out of terminate()")
        self.assertIsInstance(first.exception(0), SystemExit)
        self.assertIsInstance(second.exception(0), BrokenPipeError)

    def test_a_raising_event_callback_is_logged(self):
        def bad(event, data):
            raise ValueError("a caller's bug")

        self.inter.callback = bad
        try:
            with self.assertLogs("mpv-jsonipc", "ERROR"):
                self.inter.event_callback({"event": "pause"})
        except ValueError:
            self.fail("the event callback's exception escaped event_callback")

    def test_a_command_after_the_reader_died_fails_at_once(self):
        # The reader's quit sweeps before anything stops the socket, so a
        # send can still "succeed" into a dead connection in between.
        self.inter._on_quit()
        with mock.patch.object(python_mpv_jsonipc, "TIMEOUT", 2):
            with self.assertRaises(BrokenPipeError):
                self.inter.command("get_property", "pause")

    def test_a_send_that_fails_is_reported_to_the_callback(self):
        seen = []
        self.inter.socket.send.side_effect = BrokenPipeError("gone")
        self.inter.command_async(lambda e, r: seen.append(e), "stop")
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], BrokenPipeError)



@unittest.skipIf(os.name == "nt", "the reader there is a named pipe")
class RealReaderTest(unittest.TestCase):
    """The stub above cannot show whether the read loop goes on afterwards;
    a real UnixSocket reader against a fake mpv can."""

    def test_an_exiting_event_callback_does_not_end_the_read_loop(self):
        path = os.path.join(tempfile.mkdtemp(), "mpv.sock")
        server = socket.socket(socket.AF_UNIX)
        server.bind(path)
        server.listen(1)
        self.addCleanup(server.close)
        accepted = []
        threading.Thread(target=lambda: accepted.append(server.accept()[0]),
                         daemon=True).start()

        def leave(event, data):
            raise SystemExit(3)

        inter = python_mpv_jsonipc.MPVInter(path, callback=leave)
        self.addCleanup(inter.stop)
        self.assertTrue(_wait(lambda: accepted, 5))
        peer = accepted[0]
        self.addCleanup(peer.close)
        seen = []
        inter.command_async(lambda e, r: seen.append(r),
                            "get_property", "pause")
        request = json.loads(peer.recv(4096).split(b"\n")[0])
        with self.assertLogs("mpv-jsonipc", "ERROR"):
            peer.sendall(b'{"event": "pause"}\n')
            peer.sendall(json.dumps({"request_id": request["request_id"],
                                     "error": "success", "data": 7})
                         .encode() + b"\n")
            self.assertTrue(_wait(lambda: seen, 3),
                            "the reply after the event was never read")
        self.assertEqual(seen, [7])
        self.assertTrue(inter.socket.is_alive())


class _BadRepr:
    """A task's object whose repr raises: logging it must not cost the
    tasks queued after it."""

    def __repr__(self):
        raise ValueError("bad repr")

    def method(self):
        pass

    def raises(self):
        raise RuntimeError("a caller's bug")


class EventThreadTest(unittest.TestCase):
    def records(self):
        seen = []
        handler = logging.Handler()
        handler.emit = seen.append
        logger = logging.getLogger("mpv-jsonipc")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        return seen

    def test_a_callback_ending_the_thread_warns_once(self):
        seen = self.records()
        handler = python_mpv_jsonipc.EventHandler()
        handler.start()
        handler.put_task(sys.exit, 3)
        handler.join(5)
        self.assertFalse(handler.is_alive())
        warnings = [r for r in seen if r.levelno == logging.WARNING]
        self.assertEqual(len(warnings), 1, [r.getMessage() for r in seen])

    def test_an_ordinary_stop_does_not_warn(self):
        seen = self.records()
        handler = python_mpv_jsonipc.EventHandler()
        handler.start()
        handler.stop(5)
        self.assertEqual([r for r in seen if r.levelno >= logging.WARNING], [])

    def test_a_task_whose_repr_raises_strands_nothing_behind_it(self):
        seen = []
        handler = python_mpv_jsonipc.EventHandler()
        handler.put_task(sys.exit, 3)
        handler.put_task(_BadRepr().method)
        handler.put_task(seen.append, "after")
        self.records()
        handler.start()
        handler.join(5)
        self.assertEqual(seen, ["after"])

    def test_a_raising_task_whose_repr_raises_does_not_end_the_thread(self):
        seen = self.records()
        handler = python_mpv_jsonipc.EventHandler()
        handler.start()
        self.addCleanup(handler.stop, 5)
        handler.put_task(_BadRepr().raises)
        self.assertTrue(_wait(lambda: seen, 5))
        time.sleep(0.1)
        self.assertTrue(handler.is_alive(), "the event thread ended")
        self.assertEqual([r.levelno for r in seen], [logging.ERROR])

    def test_what_was_queued_behind_an_exit_still_runs(self):
        # The first exit ends run(); the rest goes to _drain, which must
        # survive the second and still pass the third its arguments.
        seen = []
        handler = python_mpv_jsonipc.EventHandler()
        handler.put_task(sys.exit, 3)
        handler.put_task(sys.exit, 4)
        handler.put_task(seen.append, "after")
        with self.assertLogs("mpv-jsonipc", "ERROR") as logs:
            handler.start()
            handler.join(5)
        self.assertFalse(handler.is_alive())
        self.assertEqual(seen, ["after"])
        self.assertTrue(any(r.exc_info and r.exc_info[0] is SystemExit
                            for r in logs.records))


if __name__ == "__main__":
    unittest.main()
