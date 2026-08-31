import threading
import socket
import json
import os
import re
import time
import subprocess
import random
import queue
import logging

log = logging.getLogger('mpv-jsonipc')

if os.name == "nt":
    import ctypes
    import ctypes.wintypes as _w

    # The Windows transport calls kernel32 directly rather than going through
    # ``_winapi`` and ``multiprocessing.connection.PipeConnection``. The second
    # of those is a live bug, not tidiness:
    #
    #   PipeConnection says outright that "a connection should only be used by
    #   a single thread", and this library used it from two -- a reader parked
    #   in recv_bytes, and whoever called stop(). Its _close() cancels a
    #   pending *send* before closing the handle, but has no equivalent for a
    #   pending *read*, because _recv_bytes never stores its OVERLAPPED
    #   anywhere reachable. So stop() closed the handle with an overlapped
    #   read outstanding and a kernel-owned buffer still live, which is
    #   undefined: the kernel may complete the I/O into memory Python is
    #   tearing down. CPython 3.13+ allocates its thread primitives from the
    #   parking lot, so that corruption now lands as an abort --
    #   ``Fatal Python error: _PySemaphore_Wakeup: ReleaseSemaphore failed
    #   (error: 6)`` -- seen in CI on both 3.13 and 3.14.
    #
    # Neither ``_winapi`` (private C extension) nor ``PipeConnection`` (absent
    # from __all__, defined conditionally, an implementation detail of Pipe())
    # carries a compatibility contract, so this also removes a dependency that
    # breaks along the *Python* axis rather than ours. ctypes is stdlib and
    # needs no PyInstaller hooks or bundled DLLs, which is the property the
    # original _winapi choice was buying and that downstream projects such as
    # SyncPlay and jellyfin-mpv-shim adopted this library for.
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _OPEN_EXISTING = 3
    _FILE_FLAG_OVERLAPPED = 0x40000000
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _INFINITE = 0xFFFFFFFF
    _WAIT_OBJECT_0 = 0
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_PATH_NOT_FOUND = 3
    _ERROR_BROKEN_PIPE = 109
    # Also "the other end is gone", and reached when the peer disconnects
    # rather than exits. Without them a normal disconnect logs an ERROR and a
    # traceback. Measured on real Windows, including against the old
    # PipeConnection build, which logged it too -- it only raised EOFError for
    # 109 -- so this is an improvement on the previous behaviour rather than a
    # regression being repaired. Real MPV exiting gives 109, which is why no
    # CI leg ever showed it.
    _ERROR_NO_DATA = 232
    _ERROR_PIPE_NOT_CONNECTED = 233
    _ERROR_MORE_DATA = 234
    _ERROR_OPERATION_ABORTED = 995
    _ERROR_IO_PENDING = 997

    #: Every way the far end can be gone. All of them are a clean end of
    #: stream, not an error to log.
    _PEER_GONE = (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA, _ERROR_PIPE_NOT_CONNECTED)

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p),
                    ("InternalHigh", ctypes.c_void_p),
                    ("Offset", _w.DWORD),
                    ("OffsetHigh", _w.DWORD),
                    ("hEvent", _w.HANDLE)]

    # Declared explicitly: without argtypes ctypes truncates pointers to int
    # on 64-bit, which fails in ways that look like random handle errors.
    _kernel32.CreateFileW.argtypes = [_w.LPCWSTR, _w.DWORD, _w.DWORD,
                                      ctypes.c_void_p, _w.DWORD, _w.DWORD,
                                      _w.HANDLE]
    _kernel32.CreateFileW.restype = _w.HANDLE
    _kernel32.CreateEventW.argtypes = [ctypes.c_void_p, _w.BOOL, _w.BOOL,
                                       _w.LPCWSTR]
    _kernel32.CreateEventW.restype = _w.HANDLE
    _kernel32.SetEvent.argtypes = [_w.HANDLE]
    _kernel32.SetEvent.restype = _w.BOOL
    _kernel32.ResetEvent.argtypes = [_w.HANDLE]
    _kernel32.ResetEvent.restype = _w.BOOL
    _kernel32.ReadFile.argtypes = [_w.HANDLE, ctypes.c_void_p, _w.DWORD,
                                   _w.LPDWORD, ctypes.c_void_p]
    _kernel32.ReadFile.restype = _w.BOOL
    _kernel32.WriteFile.argtypes = [_w.HANDLE, ctypes.c_void_p, _w.DWORD,
                                    _w.LPDWORD, ctypes.c_void_p]
    _kernel32.WriteFile.restype = _w.BOOL
    _kernel32.GetOverlappedResult.argtypes = [_w.HANDLE, ctypes.c_void_p,
                                              _w.LPDWORD, _w.BOOL]
    _kernel32.GetOverlappedResult.restype = _w.BOOL
    _kernel32.CancelIoEx.argtypes = [_w.HANDLE, ctypes.c_void_p]
    _kernel32.CancelIoEx.restype = _w.BOOL
    _kernel32.WaitForMultipleObjects.argtypes = [_w.DWORD, ctypes.c_void_p,
                                                 _w.BOOL, _w.DWORD]
    _kernel32.WaitForMultipleObjects.restype = _w.DWORD
    _kernel32.CloseHandle.argtypes = [_w.HANDLE]
    _kernel32.CloseHandle.restype = _w.BOOL
    _kernel32.WaitNamedPipeW.argtypes = [_w.LPCWSTR, _w.DWORD]
    _kernel32.WaitNamedPipeW.restype = _w.BOOL

TIMEOUT = 120

# Older MPV versions do not allow us to dynamically retrieve the command list.
FALLBACK_COMMAND_LIST = [
    'ignore', 'seek', 'revert-seek', 'quit', 'quit-watch-later', 'stop', 'frame-step', 'frame-back-step',
    'playlist-next', 'playlist-prev', 'playlist-shuffle', 'playlist-unshuffle', 'sub-step', 'sub-seek',
    'print-text', 'show-text', 'expand-text', 'expand-path', 'show-progress', 'sub-add', 'audio-add',
    'video-add', 'sub-remove', 'audio-remove', 'video-remove', 'sub-reload', 'audio-reload', 'video-reload',
    'rescan-external-files', 'screenshot', 'screenshot-to-file', 'screenshot-raw', 'loadfile', 'loadlist',
    'playlist-clear', 'playlist-remove', 'playlist-move', 'run', 'subprocess', 'set', 'change-list', 'add',
    'cycle', 'multiply', 'cycle-values', 'enable-section', 'disable-section', 'define-section', 'ab-loop',
    'drop-buffers', 'af', 'vf', 'af-command', 'vf-command', 'ao-reload', 'script-binding', 'script-message',
    'script-message-to', 'overlay-add', 'overlay-remove', 'osd-overlay', 'write-watch-later-config',
    'hook-add', 'hook-ack', 'mouse', 'keybind', 'keypress', 'keydown', 'keyup', 'apply-profile',
    'load-script', 'dump-cache', 'ab-loop-dump-cache', 'ab-loop-align-cache']

class MPVError(Exception):
    """An error originating from MPV or due to a problem with MPV."""
    def __init__(self, *args, **kwargs):
        super(MPVError, self).__init__(*args, **kwargs)

class MPVProcessError(MPVError):
    """MPV would not start, with whatever MPV said about why.

    A subclass so that every existing ``except MPVError`` keeps working
    unchanged -- this library is depended on by projects that will never be
    updated, and several of them catch only that.

    *returncode* is MPV's exit status, if it got far enough to have one.
    *bad_option* is the option MPV refused, when that could be determined.
    *log_output* is what MPV wrote about the failure.
    *retryable* is False when starting again cannot possibly work.
    *argv* is the command line that was tried.
    """
    def __init__(self, message, returncode=None, bad_option=None,
                 log_output=None, retryable=True, argv=None):
        super(MPVProcessError, self).__init__(message)
        self.returncode = returncode
        self.bad_option = bad_option
        self.log_output = log_output
        self.retryable = retryable
        self.argv = argv

#: MPV's own wording when it is handed an option it does not have. Matching
#: it is what turns "MPV exited" into "MPV has no --input-gamepad".
_OPTION_ERROR = re.compile(r"Error parsing option ([^\s]+) \(option not found\)")

def _diagnose_start_failure(argv, timeout=15):
    """Ask MPV why it refused *argv*. Returns ``(bad_option, output)``.

    Three details make this reliable, and each was measured rather than
    reasoned about:

    * **The message comes back on stdout, not stderr.** MPV writes its
      terminal output to stdout; stderr is empty. Capturing the wrong stream
      gets a confident, permanent "no idea why".
    * **``--terminal=yes`` has to come first.** Options are parsed in order
      and this library sets ``--terminal=no`` by default, so with the flags
      left as they are MPV has already silenced itself by the time it reaches
      the option it objects to. The library's own terminal flags are dropped
      rather than fought with.
    * **``--version`` goes last**, so a configuration that turns out to be
      fine parses everything and then exits (~15ms) instead of starting up
      and having to be killed. It exits before binding the IPC socket, so
      this cannot disturb the real MPV that is about to start.

    An earlier version of this read ``--log-file`` instead, on the theory
    that Windows has no stderr to capture. That log is written
    **unreliably**: MPV does not flush it before exiting, so it arrives
    truncated at a random point -- measured at 6/8 and 7/8 runs containing
    the error, cut off anywhere from 102 bytes in. A pipe has no such
    problem, and is byte-identical across runs.

    Every failure here is answered with ``(None, ...)``, which puts the
    caller back on the path it took before this existed. Being unable to
    explain a failed start must never turn into a second failure.
    """
    terminal_flags = ("--terminal=", "--input-terminal=")
    argv = list(argv)
    probe = ([argv[0], "--terminal=yes"]
             + [a for a in argv[1:] if not a.startswith(terminal_flags)]
             + ["--version"])
    try:
        completed = subprocess.run(probe,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT,
                                   timeout=timeout)
    except Exception:
        log.debug("Could not run MPV to diagnose the failure.", exc_info=1)
        return None, None
    output = completed.stdout.decode("utf-8", "replace")
    match = _OPTION_ERROR.search(output)
    return (match.group(1) if match else None), output

class WindowsSocket(threading.Thread):
    """
    Wraps a Windows named pipe in a high-level interface. (Internal)

    Data is automatically encoded and decoded as JSON. The callback
    function will be called for each inbound message.
    """
    def __init__(self, ipc_socket, callback=None, quit_callback=None):
        """Create the wrapper.

        *ipc_socket* is the pipe name. (Not including \\\\.\\pipe\\)
        *callback(json_data)* is the function for recieving events.
        *quit_callback* is called when the socket connection dies.
        """
        # First, so Thread's own attributes are in place before ours rather
        # than landing on top of them. Thread grew a `self._handle` of its own
        # in 3.13, so the pipe handle is `_pipe`: named `_handle` and assigned
        # before this call, Thread silently replaced it with a
        # _thread._ThreadHandle and every read died in ctypes argument
        # conversion. Anything added below needs a name Thread does not use.
        threading.Thread.__init__(self)
        self.daemon = True

        ipc_socket = "\\\\.\\pipe\\" + ipc_socket
        self.callback = callback
        self.quit_callback = quit_callback
        self._stopping = False
        self._closed = False
        # Every send is held under this lock, so holding it is proof that no
        # write is in flight and the pipe is safe to close. The reader is the
        # exception and must stay one -- see run().
        self._io_lock = threading.RLock()
        # Guards the handle *values* against being used after _close cleared
        # them. Held only for the moment it takes to signal or close, never
        # across a wait.
        self._handle_lock = threading.RLock()

        access = _GENERIC_READ | _GENERIC_WRITE
        limit = 5 # Connection may fail at first. Try 5 times.
        for _ in range(limit):
            # Still five attempts, and still load-bearing: _ipc_endpoint_ready
            # does not run on the start_mpv=False attach path, and it answers
            # True for a pipe whose instances are all busy, which CreateFile
            # then refuses with ERROR_PIPE_BUSY.
            handle = _kernel32.CreateFileW(
                ipc_socket, access, 0, None, _OPEN_EXISTING,
                _FILE_FLAG_OVERLAPPED, None)
            if handle != _INVALID_HANDLE_VALUE:
                break
            time.sleep(1)
        else:
            # Deliberately not an OSError: downstream and the failure tests
            # both pin that this is what the Windows attach path raises.
            raise MPVError("Cannot connect to pipe.")
        self._pipe = handle

        # Manual-reset, so a stop stays visible to the reader and to any send
        # waiting alongside it rather than waking exactly one of them.
        self._stop_event = _kernel32.CreateEventW(None, True, False, None)
        self._read_event = _kernel32.CreateEventW(None, True, False, None)
        self._write_event = _kernel32.CreateEventW(None, True, False, None)
        if not all((self._stop_event, self._read_event, self._write_event)):
            self._close()
            raise MPVError("Cannot create pipe events.")

        if self.callback is None:
            self.callback = lambda data: None

    def _transfer(self, func, event, buf, size):
        """Run one overlapped operation, waiting for it or for *stop*.

        Returns the bytes transferred, or None if the pipe ended or *stop* cut
        the operation short.

        The GetOverlappedResult(wait=True) below is the point of this whole
        class. It does not return until the kernel has finished with *buf* and
        the OVERLAPPED -- including when we cancelled it -- so neither is ever
        released while an I/O could still write into it.
        """
        ov = _OVERLAPPED()
        ov.hEvent = event
        _kernel32.ResetEvent(event)
        moved = _w.DWORD(0)
        if func(self._pipe, buf, size, ctypes.byref(moved), ctypes.byref(ov)):
            return moved.value

        err = ctypes.get_last_error()
        if err in _PEER_GONE:
            return None
        if err != _ERROR_IO_PENDING:
            raise ctypes.WinError(err)

        # From here the kernel owns *buf* and *ov* until the operation
        # actually finishes, so EVERY exit from this block -- including an
        # exception -- has to go through the GetOverlappedResult below.
        # Returning or raising without it destroys this frame, and its buffer,
        # with a read still outstanding, which is the corruption this class
        # exists to remove. CPython's own PipeConnection._recv_bytes uses this
        # same except/finally shape for the same reason.
        try:
            handles = (_w.HANDLE * 2)(event, self._stop_event)
            waitres = _kernel32.WaitForMultipleObjects(2, handles, False,
                                                       _INFINITE)
            if waitres != _WAIT_OBJECT_0:
                # stop() wants us gone, or the wait itself failed. Either way
                # we will not be consuming this operation.
                _kernel32.CancelIoEx(self._pipe, ctypes.byref(ov))
                if waitres != _WAIT_OBJECT_0 + 1:
                    raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            # BaseException, not Exception: a KeyboardInterrupt delivered at
            # any bytecode boundary above must not escape with I/O pending.
            _kernel32.CancelIoEx(self._pipe, ctypes.byref(ov))
            raise
        finally:
            # Cancelling only *asks*. This is the wait that makes it true.
            completed = _kernel32.GetOverlappedResult(
                self._pipe, ctypes.byref(ov), ctypes.byref(moved), True)
            failure = 0 if completed else ctypes.get_last_error()

        if not completed:
            if failure in _PEER_GONE or failure == _ERROR_OPERATION_ABORTED:
                return None
            if failure != _ERROR_MORE_DATA:
                raise ctypes.WinError(failure)
        return moved.value

    def _close(self):
        """Close the handles, once, with no I/O outstanding."""
        # _io_lock first: holding it means no send is mid-write. _handle_lock
        # second, to exclude a concurrent stop() signalling an event we are
        # about to close. Nothing ever takes these in the other order, and
        # _handle_lock is never held across a wait, so stop() cannot block.
        with self._io_lock, self._handle_lock:
            if self._closed:
                return
            self._closed = True
            for name in ("_pipe", "_stop_event", "_read_event",
                         "_write_event"):
                handle = getattr(self, name, None)
                if handle:
                    _kernel32.CloseHandle(handle)
                # Cleared, not just closed: terminate() is routinely called
                # twice, and a closed handle value can be reused by the OS.
                setattr(self, name, None)

    def stop(self, join=True):
        """Terminate the thread."""
        self._stopping = True
        # Deliberately does NOT close the handle. Closing it from here with
        # the reader's overlapped read still pending is the corruption this
        # class was rewritten to remove; instead we wake the reader and let it
        # close the handle it owns, once its read has provably finished.
        with self._handle_lock:
            if self._stop_event:
                _kernel32.SetEvent(self._stop_event)
        # `self is not current_thread()`, not is_alive(): is_alive() is True
        # for the calling thread, so it never prevented the self-join it looks
        # like it prevents. The reader reaches here whenever a user's
        # quit_callback calls terminate(), and joining yourself is a
        # RuntimeError that would escape run() and leak the event handler.
        if join and self is not threading.current_thread():
            self.join()
        if not self.is_alive():
            # The reader has finished, or never ran. Either way nothing can
            # have I/O outstanding, so the handle is ours to close.
            self._close()

    def send(self, data):
        """Send *data* to the pipe, encoded as JSON."""
        blob = json.dumps(data).encode('utf-8') + b'\n'
        with self._io_lock:
            if self._closed:
                raise BrokenPipeError("handle is closed")
            written = self._transfer(_kernel32.WriteFile, self._write_event,
                                     blob, len(blob))
        if written is None:
            raise BrokenPipeError("handle is closed")

    def run(self):
        """Process pipe events. Do not run this directly. Use *start*."""
        buf = ctypes.create_string_buffer(2048)
        data = b''
        try:
            while not self._stopping:
                # The stop event is only observed when a read *pends*, and a
                # read that completes synchronously returns before ever
                # reaching the wait. A peer with data always available could
                # therefore keep stop() waiting in join(). Not reproducible --
                # 124k messages of flooding still let the reader park -- but
                # the flag costs nothing and closes the argument.
                #
                # Emphatically NOT under _io_lock: this read is parked
                # whenever MPV has nothing to say, which is almost always, and
                # holding the lock across it would block every send. The lock
                # exists to keep a send and the close apart; the reader needs
                # no such guard because it closes the handle itself, in the
                # finally below, only once its own read has returned.
                nread = self._transfer(_kernel32.ReadFile, self._read_event,
                                       buf, len(buf))
                if not nread:
                    break

                data += buf.raw[:nread]
                if data[-1] != 10:
                    continue

                data = data.decode('utf-8', 'ignore').encode('utf-8')
                for item in data.split(b'\n'):
                    if item == b'':
                        continue
                    json_data = json.loads(item)
                    self.callback(json_data)
                data = b''
        except Exception:
            # Only log if not intentionally stopping
            if not self._stopping:
                log.error("Pipe connection died.", exc_info=1)
        finally:
            self._close()
        if self.quit_callback:
            self.quit_callback()

class UnixSocket(threading.Thread):
    """
    Wraps a Unix/Linux socket in a high-level interface. (Internal)

    Data is automatically encoded and decoded as JSON. The callback
    function will be called for each inbound message.
    """
    def __init__(self, ipc_socket, callback=None, quit_callback=None):
        """Create the wrapper.

        *ipc_socket* is the path to the socket.
        *callback(json_data)* is the function for recieving events.
        *quit_callback* is called when the socket connection dies.
        """
        self.ipc_socket = ipc_socket
        self.callback = callback
        self.quit_callback = quit_callback
        self._stopping = False
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.connect(self.ipc_socket)

        if self.callback is None:
            self.callback = lambda data: None

        threading.Thread.__init__(self)
        self.daemon = True

    def stop(self, join=True):
        """Terminate the thread."""
        self._stopping = True
        if self.socket is not None:
            try:
                self.socket.shutdown(socket.SHUT_WR)
                self.socket.close()
                self.socket = None
            except OSError:
                pass # Ignore socket close failure.
        # Not a bare `if join`: the reader thread reaches here whenever a
        # user's quit_callback calls terminate(), and joining yourself raises
        # RuntimeError -- which escaped run(), skipped the internal
        # terminate(join=False) after it, and leaked the event handler.
        if join and self is not threading.current_thread():
            self.join()

    def send(self, data):
        """Send *data* to the socket, encoded as JSON."""
        if self.socket is None:
            raise BrokenPipeError("socket is closed")
        self.socket.send(json.dumps(data).encode('utf-8') + b'\n')

    def run(self):
        """Process socket events. Do not run this directly. Use *start*."""
        data = b''
        try:
            while True:
                current_data = self.socket.recv(1024)
                if current_data == b'':
                    break

                data += current_data
                if data[-1] != 10:
                    continue

                data = data.decode('utf-8', 'ignore').encode('utf-8')
                for item in data.split(b'\n'):
                    if item == b'':
                        continue
                    json_data = json.loads(item)
                    self.callback(json_data)
                data = b''
        except Exception as ex:
            # Only log if not intentionally stopping
            if not self._stopping:
                log.error("Socket connection died.", exc_info=1)
        if self.quit_callback:
            self.quit_callback()

def _ipc_endpoint_ready(ipc_socket):
    """Return True once MPV's IPC endpoint is available.

    On POSIX the endpoint is a filesystem socket, so ``os.path.exists`` works. On
    Windows it is a *named pipe*, for which ``os.path.exists`` is always False — so we
    probe it with ``WaitNamedPipe`` instead. Using ``os.path.exists`` on Windows made
    the startup poll never detect the pipe, raising a spurious "MPV start timed out".
    """
    if os.name != 'nt':
        return os.path.exists(ipc_socket)
    if _kernel32.WaitNamedPipeW(ipc_socket, 0):
        return True
    # Both codes, because the old _winapi version raised FileNotFoundError and
    # CPython maps ERROR_PATH_NOT_FOUND to ENOENT as well. Treating 3 as
    # "present but busy" would report a successful start and then burn
    # WindowsSocket's five CreateFileW retries before failing with the wrong
    # exception for the caller's retry loop.
    if ctypes.get_last_error() in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
        return False
    # Pipe exists but all instances are momentarily busy — it is present.
    return True


class MPVProcess:
    """
    Manages an MPV process, ensuring the socket or pipe is available. (Internal)
    """
    def __init__(self, ipc_socket, mpv_location=None, discard_output=False, **kwargs):
        """
        Create and start the MPV process. Will block until socket/pipe is available.

        *ipc_socket* is the path to the Unix/Linux socket or name of the Windows pipe.
        *mpv_location* is the path to mpv. If left unset it tries the one in the PATH.
        *discard_output* sends MPV's stdout and stderr to devnull instead of inheriting
        the caller's. (Default: False)

        All other arguments are forwarded to MPV as command-line arguments.
        """
        if mpv_location is None:
            if os.name == 'nt':
                mpv_location = "mpv.exe"
            else:
                mpv_location = "mpv"

        log.debug("Staring MPV from {0}.".format(mpv_location))
        ipc_socket_name = ipc_socket
        if os.name == 'nt':
            ipc_socket = "\\\\.\\pipe\\" + ipc_socket

        if os.name != 'nt' and os.path.exists(ipc_socket):
            os.remove(ipc_socket)

        log.debug("Using IPC socket {0} for MPV.".format(ipc_socket))
        self.ipc_socket = ipc_socket
        args = [mpv_location]
        self._set_default(kwargs, "idle", True)
        self._set_default(kwargs, "input_ipc_server", ipc_socket_name)
        self._set_default(kwargs, "input_terminal", False)
        self._set_default(kwargs, "terminal", False)

        arg_pairs = []
        for key, value in kwargs.items():
            if type(value) == list:
                for v in value:
                    arg_pairs.append((key, v))
            else:
                arg_pairs.append((key, value))

        args.extend("--{0}={1}".format(v[0].replace("_", "-"), self._mpv_fmt(v[1]))
                    for v in arg_pairs)
        self.argv = args
        # close_fds only covers fds above 2, so MPV inherits our stdout and
        # stderr. It never writes to them under --terminal=no, which is why
        # this is invisible until something waits for EOF on the other end of
        # that pipe: an MPV that outlives us holds it open and that reader
        # blocks forever. Opt-in rather than default because callers who do
        # want MPV's output on their terminal have had it for years.
        stdio = subprocess.DEVNULL if discard_output else None
        self.process = subprocess.Popen(args, stdout=stdio, stderr=stdio)
        ipc_exists = False
        for _ in range(100): # Give MPV 10 seconds to start.
            time.sleep(0.1)
            self.process.poll()
            if _ipc_endpoint_ready(ipc_socket):
                ipc_exists = True
                log.debug("Found MPV socket.")
                break
            if self.process.returncode is not None:
                log.error("MPV failed with returncode {0}.".format(self.process.returncode))
                break
        else:
            # stop(), not a bare terminate(): a failed start orphans MPV
            # exactly like a failed teardown did, and MPV.__init__ retries
            # this up to start_retries times -- so a machine where MPV wedges
            # without opening its pipe used to leave five live processes,
            # each still holding the caller's stdout.
            self.stop()
            raise MPVProcessError("MPV start timed out.", argv=args)

        if not ipc_exists or self.process.returncode is not None:
            self.stop()
            # Message unchanged: it has been this string for years and is not
            # ours to break. What is new is the argv riding along, which is
            # what lets the caller ask MPV why.
            raise MPVProcessError("MPV not started.",
                                  returncode=self.process.returncode,
                                  argv=args)

    def _set_default(self, prop_dict, key, value):
        if key not in prop_dict:
            prop_dict[key] = value

    def _mpv_fmt(self, data):
        # Use identity comparison so numeric arguments are preserved. In Python
        # ``1 == True`` and ``0 == False``, so ``==`` would convert numeric
        # options such as ``video-scale-x=1`` into ``--video-scale-x=yes``,
        # which MPV rejects.
        if data is True:
            return "yes"
        elif data is False:
            return "no"
        else:
            return data

    def stop(self, timeout=5):
        """Terminate the process, and do not return while it may still be alive.

        *timeout* is the seconds to wait for a polite exit before killing, and
        again for the kill to land.
        """
        # terminate() only *asks*. Returning here while MPV is still running
        # left the caller no supported way to wait for it -- and a caller that
        # exits at that moment orphans an MPV still holding its stdout. Both
        # waits are bounded so a wedged MPV cannot hang teardown.
        self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("MPV ignored terminate for {0}s; killing it.".format(timeout))
            self.process.kill()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.error("MPV survived kill; giving up on reaping it.")
        if os.name != 'nt':
            # try/except rather than exists()-then-remove, because two threads
            # reach here: the caller's terminate(), and the reader's
            # quit_callback -> terminate(join=False) when MPV's exit closes
            # the socket. Waiting above made them *overlap by construction* --
            # the wait ends when MPV dies, which is the same event that wakes
            # the reader -- so the check-then-act window is reliably lost.
            try:
                os.remove(self.ipc_socket)
            except FileNotFoundError:
                pass # The other thread got there first, which is all we wanted.

class MPVInter:
    """
    Low-level interface to MPV. Does NOT manage an mpv process. (Internal)
    """
    def __init__(self, ipc_socket, callback=None, quit_callback=None):
        """Create the wrapper.

        *ipc_socket* is the path to the Unix/Linux socket or name of the Windows pipe.
        *callback(event_name, data)* is the function for recieving events.
        *quit_callback* is called when the socket connection to MPV dies.
        """
        Socket = UnixSocket
        if os.name == 'nt':
            Socket = WindowsSocket

        self.callback = callback
        self.quit_callback = quit_callback
        if self.callback is None:
            self.callback = lambda event, data: None

        self.socket = Socket(ipc_socket, self.event_callback, self.quit_callback)
        self.socket.start()
        self.command_id = 1
        self.rid_lock = threading.Lock()
        self.socket_lock = threading.Lock()
        self.cid_result = {}
        self.cid_wait = {}

    def stop(self, join=True):
        """Terminate the underlying connection."""
        self.socket.stop(join)

    def event_callback(self, data):
        """Internal callback for recieving events from MPV."""
        if "request_id" in data:
            self.cid_result[data["request_id"]] = data
            self.cid_wait[data["request_id"]].set()
        elif "event" in data:
            self.callback(data["event"], data)

    def command(self, command, *args):
        """
        Issue a command to MPV. Will block until completed or timeout is reached.

        *command* is the name of the MPV command

        All further arguments are forwarded to the MPV command.
        Throws TimeoutError if timeout of 120 seconds is reached.
        """
        self.rid_lock.acquire()
        command_id = self.command_id
        self.command_id += 1
        self.rid_lock.release()

        event = threading.Event()
        self.cid_wait[command_id] = event

        command_list = [command]
        command_list.extend(args)
        try:
            self.socket_lock.acquire()
            self.socket.send({"command":command_list, "request_id": command_id})
        finally:
            self.socket_lock.release()

        has_event = event.wait(timeout=TIMEOUT)
        if has_event:
            data = self.cid_result[command_id]
            del self.cid_result[command_id]
            del self.cid_wait[command_id]
            if data["error"] != "success":
                if data["error"] == "property unavailable":
                    return None
                raise MPVError(data["error"])
            else:
                return data.get("data")
        else:
            raise TimeoutError("No response from MPV.")

class EventHandler(threading.Thread):
    """Event handling thread. (Internal)"""
    def __init__(self):
        """Create an instance of the thread."""
        self.queue = queue.Queue()
        threading.Thread.__init__(self)
        self.daemon = True

    def put_task(self, func, *args):
        """
        Put a new task to the thread.

        *func* is the function to call

        All further arguments are forwarded to *func*.
        """
        self.queue.put((func, args))

    def stop(self, join=True):
        """Terminate the thread."""
        self.queue.put("quit")
        self.join(join)

    def run(self):
        """Process socket events. Do not run this directly. Use *start*."""
        while True:
            event = self.queue.get()
            if event == "quit":
                break
            try:
                event[0](*event[1])
            except Exception:
                log.error("EventHandler caught exception from {0}.".format(event), exc_info=1)

class MPV:
    """
    The main MPV interface class. Use this to control MPV.

    This will expose all mpv commands as callable methods and all properties.
    You can set properties and call the commands directly.

    Please note that if you are using a really old MPV version, a fallback command
    list is used. Not all commands may actually work when this fallback is used.
    """
    def __init__(self, start_mpv=True, ipc_socket=None, mpv_location=None,
                 log_handler=None, loglevel=None, quit_callback=None, start_retries=5, start_retry_delay_ms=1000,
                 diagnose_start_failures=True, discard_output=False, **kwargs):
        """
        Create the interface to MPV and process instance.

        *start_mpv* will start an MPV process if true. (Default: True)
        *ipc_socket* is the path to the Unix/Linux socket or name of Windows pipe. (Default: Random Temp File)
        *mpv_location* is the location of MPV for *start_mpv*. (Default: Use MPV in PATH)
        *log_handler(level, prefix, text)* is an optional handler for log events. (Default: Disabled)
        *loglevel* is the level for log messages. Levels are fatal, error, warn, info, v, debug, trace. (Default: Disabled)
        *quit_callback* is called when the socket connection to MPV dies.
        *diagnose_start_failures* re-runs MPV once, on the first failed start, to find out
        why it refused to start. If MPV names an option it does not have, MPVProcessError is
        raised immediately with *bad_option* set instead of spending every retry on something
        that cannot succeed. Set it to False for the pre-1.3 behaviour. (Default: True)
        *discard_output* sends MPV's stdout and stderr to devnull instead of letting it
        inherit the caller's. Set it if anything waits for EOF on your stdout, since an
        MPV that outlives *terminate* would otherwise hold that pipe open. Note this is
        not MPV's own --quiet, which is still forwarded as an MPV option. (Default: False)

        All other arguments are forwarded to MPV as command-line arguments if *start_mpv* is used.
        """
        self.properties = {}
        self.event_bindings = {}
        self.key_bindings = {}
        self.property_bindings = {}
        self.mpv_process = None
        self.mpv_inter = None
        self.quit_callback = quit_callback
        self.event_handler = EventHandler()
        self.event_handler.start()
        if ipc_socket is None:
            rand_file = "mpv{0}".format(random.randint(0, 2**48))
            if os.name == "nt":
                ipc_socket = rand_file
            else:
                ipc_socket = "/tmp/{0}".format(rand_file)

        if start_mpv:
            # Attempt to start MPV multiple times.
            last_error = None
            diagnosed = False
            log_output = None
            for i in range(start_retries):
                try:
                    self.mpv_process = MPVProcess(ipc_socket, mpv_location,
                                                  discard_output=discard_output, **kwargs)
                    break
                except MPVError as error:
                    last_error = error
                    log.warning("MPV start failed.", exc_info=1)

                    # Ask MPV why, once, on the first failure. An option MPV
                    # does not have cannot start on the fifth attempt either,
                    # and spending the whole retry budget on it produces the
                    # least useful error this library has ever raised: a
                    # timeout that names nothing. The probe costs one ~20ms
                    # MPV run and only ever happens on a start that has
                    # already failed.
                    argv = getattr(error, "argv", None)
                    if diagnose_start_failures and argv and not diagnosed:
                        diagnosed = True
                        bad_option, diagnosis = _diagnose_start_failure(argv)
                        # Keep what MPV said even when it does not name an
                        # option. Refusing to start over a missing DLL or an
                        # unusable VO is the same predicament from the user's
                        # side -- something they changed, and nothing to go
                        # on -- and on Windows there is no console for them to
                        # have seen it in. It rides out on .log_output below.
                        log_output = diagnosis
                        if bad_option is not None:
                            raise MPVProcessError(
                                "MPV rejected the option --{0}.".format(
                                    bad_option),
                                returncode=getattr(error, "returncode", None),
                                bad_option=bad_option,
                                log_output=log_output,
                                retryable=False,
                                argv=argv)

                    time.sleep(start_retry_delay_ms / 1000)
                    continue
            else:
                # The message is unchanged on purpose. Downstream projects
                # that will never be updated may be matching on it, and this
                # is still the same situation it has always described.
                raise MPVProcessError(
                    "MPV process retry limit reached.",
                    returncode=getattr(last_error, "returncode", None),
                    log_output=log_output,
                    argv=getattr(last_error, "argv", None))

        self.mpv_inter = MPVInter(ipc_socket, self._callback, self._quit_callback)
        self.properties = set(x.replace("-", "_") for x in self.command("get_property", "property-list"))
        try:
            command_list = [x["name"] for x in self.command("get_property", "command-list")]
        except MPVError:
            log.warning("Using fallback command list.")
            command_list = FALLBACK_COMMAND_LIST
        for command in command_list:
            command_name = command.replace("-", "_")
            if command_name in self.properties:
                command_name = f"{command_name}_cmd"
            object.__setattr__(self, command_name, self._get_wrapper(command))

        self._dir = list(self.properties)
        self._dir.extend(object.__dir__(self))

        self.observer_id = 1
        self.observer_lock = threading.Lock()
        self.keybind_id = 1
        self.keybind_lock = threading.Lock()

        if log_handler is not None and loglevel is not None:
            self.command("request_log_messages", loglevel)
            @self.on_event("log-message")
            def log_handler_event(data):
                self.event_handler.put_task(log_handler, data["level"], data["prefix"], data["text"].strip())

        @self.on_event("property-change")
        def event_handler(data):
            if data.get("id") in self.property_bindings:
                self.event_handler.put_task(self.property_bindings[data["id"]], data["name"], data.get("data"))

        @self.on_event("client-message")
        def client_message_handler(data):
            args = data["args"]
            if len(args) == 2 and args[0] == "custom-bind":
                self.event_handler.put_task(self.key_bindings[args[1]])

    def _quit_callback(self):
        """
        Internal handler for quit events.
        """
        if self.quit_callback:
            self.quit_callback()
        self.terminate(join=False)

    def bind_event(self, name, callback):
        """
        Bind a callback to an MPV event.

        *name* is the MPV event name.
        *callback(event_data)* is the function to call.
        """
        if name not in self.event_bindings:
            self.event_bindings[name] = set()
        self.event_bindings[name].add(callback)

    def on_event(self, name):
        """
        Decorator to bind a callback to an MPV event.

        @on_event(name)
        def my_callback(event_data):
            pass
        """
        def wrapper(func):
            self.bind_event(name, func)
            return func
        return wrapper

    # Added for compatibility.
    def event_callback(self, name):
        """An alias for on_event to maintain compatibility with python-mpv."""
        return self.on_event(name)

    def on_key_press(self, name):
        """
        Decorator to bind a callback to an MPV keypress event.

        @on_key_press(key_name)
        def my_callback():
            pass
        """
        def wrapper(func):
            self.bind_key_press(name, func)
            return func
        return wrapper

    def bind_key_press(self, name, callback):
        """
        Bind a callback to an MPV keypress event.

        *name* is the key symbol.
        *callback()* is the function to call.
        """
        self.keybind_lock.acquire()
        keybind_id = self.keybind_id
        self.keybind_id += 1
        self.keybind_lock.release()

        bind_name = "bind{0}".format(keybind_id)
        self.key_bindings["bind{0}".format(keybind_id)] = callback
        try:
            self.keybind(name, "script-message custom-bind {0}".format(bind_name))
        except MPVError:
            self.define_section(bind_name, "{0} script-message custom-bind {1}".format(name, bind_name))
            self.enable_section(bind_name)

    def bind_property_observer(self, name, callback):
        """
        Bind a callback to an MPV property change.

        *name* is the property name.
        *callback(name, data)* is the function to call.

        Returns a unique observer ID needed to destroy the observer.
        """
        self.observer_lock.acquire()
        observer_id = self.observer_id
        self.observer_id += 1
        self.observer_lock.release()

        self.property_bindings[observer_id] = callback
        self.command("observe_property", observer_id, name)
        return observer_id

    def unbind_property_observer(self, observer_id):
        """
        Remove callback to an MPV property change.

        *observer_id* is the id returned by bind_property_observer.
        """
        self.command("unobserve_property", observer_id)
        del self.property_bindings[observer_id]

    def property_observer(self, name):
        """
        Decorator to bind a callback to an MPV property change.

        @property_observer(property_name)
        def my_callback(name, data):
            pass
        """
        def wrapper(func):
            self.bind_property_observer(name, func)
            return func
        return wrapper

    def wait_for_property(self, name):
        """
        Waits for the value of a property to change.

        *name* is the name of the property.
        """
        event = threading.Event()
        first_event = True
        def handler(*_):
            nonlocal first_event
            if first_event == True:
                first_event = False
            else:
                event.set()
        observer_id = self.bind_property_observer(name, handler)
        event.wait()
        self.unbind_property_observer(observer_id)

    def _get_wrapper(self, name):
        def wrapper(*args):
            return self.command(name, *args)
        return wrapper

    def _callback(self, event, data):
        if event in self.event_bindings:
            for callback in self.event_bindings[event]:
                self.event_handler.put_task(callback, data)

    def play(self, url):
        """Play the specified URL. An alias to loadfile()."""
        self.loadfile(url)

    def __del__(self):
        self.terminate()

    def terminate(self, join=True):
        """Terminate the connection to MPV and process (if *start_mpv* is used)."""
        if self.mpv_process:
            # Unconditionally, regardless of *join*: that flag governs whether
            # we join our *threads*, and _quit_callback passes False only
            # because a thread cannot join itself. Letting it skip the wait
            # would hand back the orphaned-MPV bug to anyone who passes it,
            # and a terminate() that does not terminate is the worse failure.
            self.mpv_process.stop()
        if self.mpv_inter:
            self.mpv_inter.stop(join)
        self.event_handler.stop(join)

    def command(self, command, *args):
        """
        Send a command to MPV. All commands are bound to the class by default,
        except JSON IPC specific commands. This may also be useful to retain
        compatibility with python-mpv, as it does not bind all of the commands.

        *command* is the command name.

        All further arguments are forwarded to the MPV command.
        """
        return self.mpv_inter.command(command, *args)

    def __getattr__(self, name):
        if name in self.properties:
            return self.command("get_property", name.replace("_", "-"))
        return object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        if name not in {"properties", "command"} and name in self.properties:
            return self.command("set_property", name.replace("_", "-"), value)
        return object.__setattr__(self, name, value)

    def __hasattr__(self, name):
        if object.__hasattr__(self, name):
            return True
        else:
            try:
                getattr(self, name)
                return True
            except MPVError:
                return False

    def __dir__(self):
        return self._dir
