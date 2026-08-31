r"""Exercise `WindowsSocket` against a loopback named pipe, with no mpv.

Not part of the unittest suite (`discover` only collects `test*.py`), because
it needs a Windows interpreter -- real or wine. It has two jobs: let the
transport be *run* while editing it from a Linux machine, and give the
`windows-stress` CI leg something it can repeat tens of thousands of times
without paying for an mpv start each round.

    python tests/pipe_transport_check.py 20000        # on Windows

    # ...or from Linux, against a Windows interpreter under wine:
    WINEPREFIX=/tmp/winepfx WINEARCH=win64 wineboot -i
    unzip python-3.13.x-embed-amd64.zip -d "$WINEPREFIX/drive_c/pyembed"
    LIBDIR='Z:\path\to\repo' wine C:\\pyembed\\python.exe \
        Z:/path/to/tests/pipe_transport_check.py 500

It stands in for mpv with a plain named-pipe server, so it covers the
transport and nothing above it -- no argv building, no property machinery.

**What it proves.** That the ctypes prototypes match kernel32, that a
round-trip works in both directions, and that the teardown paths behave: the
error shape when nothing is listening, `BrokenPipeError` after `stop()`,
`quit_callback` on a dead peer, concurrent sends not deadlocking against the
reader, and `stop(join=False)` still ending the thread. It caught a real one:
`Thread` owns `self._handle` on 3.13+, so the pipe handle had to be renamed.

**Under wine it says nothing about memory safety.** The corruption needs the
Windows kernel completing an I/O into a freed buffer; wine's named pipes are
its own implementation, and the *old* PipeConnection code also survives 2000
cycles of this harness there. So a green wine run is evidence about behaviour
only -- which is exactly why the same file is run at high cycle counts on a
real Windows runner, where the teardown it repeats is the one that aborted.
Even there, absence of an abort is weak evidence: the observed rate was
roughly 1 in 850 teardowns, so cycle count is the whole point.
"""

import ctypes
import json
import os
import sys
import threading
import time

if os.name != "nt":
    sys.exit("This must run on a Windows interpreter (wine is fine).")

import ctypes.wintypes as w  # noqa: E402

sys.path.insert(0, os.environ.get("LIBDIR", os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import python_mpv_jsonipc as m  # noqa: E402

_INVALID = ctypes.c_void_p(-1).value
_ERROR_PIPE_CONNECTED = 535

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateNamedPipeW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD,
                                  w.DWORD, w.DWORD, w.DWORD, ctypes.c_void_p]
_k32.CreateNamedPipeW.restype = w.HANDLE
_k32.ConnectNamedPipe.argtypes = [w.HANDLE, ctypes.c_void_p]
_k32.ReadFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, w.LPDWORD,
                          ctypes.c_void_p]
_k32.WriteFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, w.LPDWORD,
                           ctypes.c_void_p]
_k32.DisconnectNamedPipe.argtypes = [w.HANDLE]
_k32.DisconnectNamedPipe.restype = w.BOOL
_k32.CloseHandle.argtypes = [w.HANDLE]


class Peer(threading.Thread):
    """A blocking-mode pipe server, standing in for mpv."""

    daemon = True

    def __init__(self, name):
        threading.Thread.__init__(self)
        self.handle = _k32.CreateNamedPipeW(
            r"\\.\pipe" + "\\" + name, 3, 0, 1, 65536, 65536, 0, None)
        assert self.handle != _INVALID, ctypes.get_last_error()
        self.received = []
        self.ready = threading.Event()

    def run(self):
        if not _k32.ConnectNamedPipe(self.handle, None):
            if ctypes.get_last_error() != _ERROR_PIPE_CONNECTED:
                return
        self.ready.set()
        buf = ctypes.create_string_buffer(4096)
        read = w.DWORD(0)
        while _k32.ReadFile(self.handle, buf, 4096, ctypes.byref(read),
                            None) and read.value:
            self.received.append(buf.raw[:read.value])

    def write(self, obj):
        blob = json.dumps(obj).encode("utf-8") + b"\n"
        written = w.DWORD(0)
        _k32.WriteFile(self.handle, blob, len(blob), ctypes.byref(written),
                       None)

    def close(self):
        # Disconnect, join, THEN close. Closing the handle while this peer's
        # own thread is parked in a blocking ReadFile on it is precisely the
        # pattern this harness exists to prove absent from the library -- and
        # at 50k cycles on a real runner, a harness-side abort or stuck thread
        # would be blamed on WindowsSocket, which is the one thing this must
        # never do.
        _k32.DisconnectNamedPipe(self.handle)
        self.join(5)
        _k32.CloseHandle(self.handle)


_results = []
_running = ["nothing yet"]


def check(label, condition, detail=""):
    _results.append(bool(condition))
    # Unbuffered: the failure mode worth catching here is a hang, and a
    # buffered report dies with the process that was supposed to print it.
    print("  %s  %s%s" % ("PASS" if condition else "FAIL", label,
                          "" if condition else "   <- " + str(detail)),
          flush=True)


def watchdog(seconds):
    """Turn a deadlock into a named failure instead of a silent hang.

    Every wait in here is meant to be short. If one is not, the interesting
    information is *which* one, and a caller that has to kill us learns
    nothing -- so say it and leave hard, since the point of the deadlock this
    guards is that the threads holding it will never come back to be joined.
    """
    def bark():
        time.sleep(seconds)
        print("\n  FAIL  timed out after %ds during %s"
              % (seconds, _running[0]), flush=True)
        os._exit(2)

    thread = threading.Thread(target=bark)
    thread.daemon = True
    thread.start()


def connected(name):
    """A started Peer and a started WindowsSocket talking to it."""
    peer = Peer(name)
    peer.start()
    inbox = []
    sock = m.WindowsSocket(name, callback=inbox.append)
    sock.start()
    peer.ready.wait(5)
    return peer, sock, inbox


def test_round_trip():
    peer, sock, inbox = connected("wine-roundtrip")
    peer.write({"event": "hello", "n": 7})
    for _ in range(500):
        if inbox:
            break
        time.sleep(0.002)
    check("an inbound event reaches the callback",
          inbox and inbox[0] == {"event": "hello", "n": 7}, inbox)

    sock.send({"command": ["get_property", "pause"], "request_id": 7})
    for _ in range(500):
        if peer.received:
            break
        time.sleep(0.002)
    check("an outbound command reaches the peer",
          peer.received and json.loads(peer.received[0])["request_id"] == 7,
          peer.received)
    sock.stop()
    peer.close()


def test_no_listener():
    # Pinned by test_live_failure.py: unlike the Unix transport, this raises
    # MPVError rather than OSError, and takes five seconds to do it.
    started = time.time()
    try:
        m.WindowsSocket("wine-nothing-is-listening-here")
        check("connecting to nothing raises", False, "it returned")
    except Exception as error:
        elapsed = time.time() - started
        check("connecting to nothing raises MPVError",
              isinstance(error, m.MPVError), repr(error))
        check("...and deliberately not an OSError",
              not isinstance(error, OSError), repr(error))
        check("...after the five one-second retries",
              4.0 < elapsed < 12.0, "%.1fs" % elapsed)


def test_endpoint_readiness():
    peer = Peer("wine-readiness")
    peer.start()
    check("_ipc_endpoint_ready sees a live pipe",
          m._ipc_endpoint_ready(r"\\.\pipe\wine-readiness"))
    check("_ipc_endpoint_ready does not see a missing one",
          not m._ipc_endpoint_ready(r"\\.\pipe\wine-readiness-absent"))
    peer.close()


def test_send_after_stop():
    peer, sock, _ = connected("wine-after-stop")
    sock.stop()
    try:
        sock.send({"command": ["get_property", "pause"]})
        check("send after stop raises BrokenPipeError", False, "it returned")
    except BrokenPipeError:
        check("send after stop raises BrokenPipeError", True)
    except Exception as error:
        check("send after stop raises BrokenPipeError", False, repr(error))
    peer.close()


def test_quit_callback():
    fired = threading.Event()
    peer = Peer("wine-peer-dies")
    peer.start()
    sock = m.WindowsSocket("wine-peer-dies", callback=lambda data: None,
                           quit_callback=fired.set)
    sock.start()
    peer.ready.wait(5)
    peer.close()
    check("quit_callback fires when the peer disappears", fired.wait(5))


def test_concurrent_sends():
    # The reader parks in an overlapped read for as long as mpv is quiet. If
    # it held the I/O lock across that wait, every send here would block
    # until mpv happened to say something.
    peer, sock, _ = connected("wine-concurrent")
    errors = []

    def spam(base):
        try:
            for i in range(50):
                sock.send({"command": ["x"], "request_id": base * 1000 + i})
        except Exception as error:  # pragma: no cover - failure path
            errors.append(error)

    threads = [threading.Thread(target=spam, args=(n,)) for n in range(4)]
    started = time.time()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    check("200 concurrent sends finish while the reader is parked",
          not any(t.is_alive() for t in threads) and not errors,
          "errors=%s" % errors)
    check("...without taking the scenic route",
          time.time() - started < 20, "%.1fs" % (time.time() - started))
    sock.stop()
    peer.close()


def test_stop_without_join():
    peer, sock, _ = connected("wine-nojoin")
    sock.stop(join=False)
    deadline = time.time() + 5
    while sock.is_alive() and time.time() < deadline:
        time.sleep(0.01)
    check("stop(join=False) still ends the reader thread", not sock.is_alive())
    peer.close()


def test_teardown_cycles(rounds):
    """Construct and stop many times, always with a read pending.

    This is the shape that aborted the interpreter on real Windows. It does
    not reproduce here -- see the module docstring -- so treat it as a leak
    and hang check, not as evidence about the corruption.
    """
    started = time.time()
    baseline = threading.active_count()
    for i in range(rounds):
        peer, sock, inbox = connected("cycle-%d-%d" % (os.getpid(), i))
        peer.write({"event": "tick", "n": i})
        for _ in range(100):
            if inbox:
                break
            time.sleep(0.001)
        sock.stop()
        peer.close()
    check("%d construct/stop cycles with a read pending" % rounds, True, "")
    # Asserted, not merely printed: a harness that leaks its own peer threads
    # would otherwise quietly turn a long run into a thread-exhaustion failure
    # and look like a library bug.
    check("the harness left no threads behind",
          threading.active_count() <= baseline,
          "baseline %d, now %d" % (baseline, threading.active_count()))
    print("       (%.1fs)" % (time.time() - started))


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    print("library under test:", m.__file__, flush=True)
    watchdog(int(os.environ.get("TRANSPORT_CHECK_TIMEOUT", "180")))
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and name != "test_teardown_cycles":
            _running[0] = name
            print(name, flush=True)
            func()
    _running[0] = "test_teardown_cycles"
    print("test_teardown_cycles", flush=True)
    test_teardown_cycles(rounds)

    failed = _results.count(False)
    print("\n%d passed, %d failed" % (_results.count(True), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
