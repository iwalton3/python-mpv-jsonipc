# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`python-mpv-jsonipc` is a single-file Python library (`python_mpv_jsonipc.py`) that controls MPV via its JSON IPC protocol over a Unix socket or Windows named pipe. It mimics the subset of the `python-mpv` API used by `plex-mpv-shim` and `jellyfin-mpv-shim` — feature parity with `python-mpv` is *not* a goal. No runtime dependencies; stdlib only.

## Build / package

```bash
python3 -m build       # build sdist + wheel into dist/
pip3 install -e .      # editable install for local testing
```

Packaging metadata lives in `pyproject.toml` (PEP 621 + PEP 639 SPDX license expression). There is no `setup.py` — do not reintroduce one. There is no linter config.

## Architecture

The whole library is one module organized as four cooperating layers, bottom-up:

1. **Transport** — `UnixSocket` / `WindowsSocket` (both `threading.Thread` subclasses). Frame newline-delimited JSON to/from the MPV IPC endpoint and call back per-message. Selected at runtime by `os.name`.
2. **Process** — `MPVProcess` spawns and supervises an MPV child process when `start_mpv=True`. Translates Python kwargs into `--key=value` MPV CLI args (underscores → dashes; `True/False` → `yes/no`; lists expand to repeated flags). Polls for the socket/pipe to appear before returning.
3. **RPC** — `MPVInter` wraps a transport and implements request/response correlation via a monotonic `request_id` plus a per-id `threading.Event`. `command()` blocks up to `TIMEOUT` (120s) for the matching reply. Events (no `request_id`) are dispatched to a callback.
4. **Public API** — `MPV` is what users instantiate. On construction it queries `property-list` and `command-list` from the live MPV and dynamically attaches every command as a method and every property as an attribute (`__getattr__` / `__setattr__` translate to `get_property` / `set_property`). Property and command names use underscores in Python and are converted to dashes on the wire. If a command name collides with a property name, the command method is suffixed with `_cmd`.

A separate `EventHandler` thread serializes user callbacks (events, property observers, key-press handlers) so MPV's read loop is never blocked by user code. Property observers and custom key bindings each get a unique integer ID; key bindings round-trip through MPV's `script-message custom-bind <id>` mechanism, with a `define_section`/`enable_section` fallback for older MPV builds that reject `keybind`.

### Things that surprise

- **`FALLBACK_COMMAND_LIST`** at the top of the file is used only when `get_property command-list` fails (very old MPV). When editing, remember it must stay roughly in sync with what real MPV exposes, but it is never the source of truth on modern MPV.
- **`start_retries` / `start_retry_delay_ms`** on `MPV.__init__` retry the *whole* `MPVProcess` construction (including the 10-second socket-appearance poll inside `MPVProcess`).
- **`wait_for_property`** ignores the first observer event because MPV emits an immediate "current value" notification on `observe_property`; only the *next* change releases the wait.
- **`__del__` calls `terminate()`** — be careful adding state that isn't safe to touch during interpreter shutdown.
- **`WindowsSocket`'s five-attempt `CreateFile` retry is not redundant with
  `_ipc_endpoint_ready`**, even though it looks it now. The readiness poll
  added in #23 runs inside `MPVProcess`, so on the ordinary start path the
  pipe is known to exist before the transport is built — but two paths still
  depend on the retry alone. (1) **`start_mpv=False`** builds no `MPVProcess`
  at all, so nothing polls; the retry is the whole readiness wait when
  attaching to an mpv somebody else started (jellyfin-mpv-shim's
  `mpv_ext_start: false`). (2) `_ipc_endpoint_ready` deliberately answers
  *True* for a pipe that exists but whose instances are all busy — see its
  own comment — and `CreateFile` against a busy pipe fails with
  `ERROR_PIPE_BUSY`, an `OSError`. The retry is what turns that race into a
  connection a moment later rather than a hard failure. Keep it.
- **FIXED 2026-08-31: the Windows transport used to abort the interpreter.**
  `Fatal Python error: _PySemaphore_Wakeup: parking_lot: ReleaseSemaphore
  failed (error: 6)` — ERROR_INVALID_HANDLE on a *CPython-internal* semaphore,
  i.e. handle/heap corruption rather than an error path. Seen twice in CI,
  on **3.14** (2026-08-18) and **3.13** (2026-08-31), always under
  `terminate()` → `MPVInter.stop` → `WindowsSocket.stop` → `join`. It was not
  a 3.14 regression: 3.13+ allocates thread primitives from the parking lot,
  which changed where the corruption *lands*, not whether it happens. Older
  Pythons were quieter, not safer.
  The mechanism was `PipeConnection._recv_bytes` parking the reader in
  `WaitForMultipleObjects([ov.event], INFINITE)` with an **overlapped read
  still pending** and a kernel-owned buffer inside `ov`, while
  `WindowsSocket.stop` closed that handle **from another thread**. Note this
  was CPython's gap as much as ours: `PipeConnection._close` cancels a pending
  *send* before closing but has no equivalent for a pending *read*, and the
  class says outright that it "should only be used by a single thread".
  **The fix is structural, so do not undo its shape.** `stop()` now signals an
  event; the reader cancels its own read, waits for
  `GetOverlappedResult(wait=True)` to confirm the kernel is finished with the
  buffer, and closes the handle it owns. Nothing is ever closed with I/O
  outstanding. Do not "simplify" this back to closing from `stop()`, and do
  not bound the join instead — that only hides the abort behind a timeout.

- **The Windows transport is `ctypes` against `kernel32`** (`_transfer`,
  `_close`, `_ipc_endpoint_ready`), which replaced `_winapi` plus
  `multiprocessing.connection.PipeConnection` in the fix above. Neither of
  those carried a compatibility contract — `_winapi` is a private C extension
  and `PipeConnection` is absent from `__all__`, defined conditionally, and an
  implementation detail of `Pipe()` — so the transport broke along the
  *Python* axis rather than ours, which is why the Windows matrix runs on a
  schedule with prereleases. ctypes keeps the property the original choice was
  actually buying over `pywin32`: stdlib, no hooks, no `pythoncom`/
  `pywintypes` DLLs, PyInstaller-clean, which is why downstream projects
  adopted this library. `FILE_FLAG_OVERLAPPED` is still essential — a plain
  `open(r'\\.\pipe\...')` cannot do overlapped I/O.
  **The reader thread must never hold `_io_lock` across its read.** That lock
  exists only to keep a `send` and the close apart; held across the blocking
  read it deadlocks every send until MPV happens to speak.
  **Do not name any attribute `_handle`.** `threading.Thread` owns that name
  from 3.13 on, so the pipe handle is `_pipe`, and `Thread.__init__` is called
  *first* so its attributes cannot land on top of ours.

## Testing

```bash
python3 -m unittest discover tests          # from the repo root
MPV_BINARY=/path/to/mpv python3 -m unittest discover tests
```

Stdlib `unittest`, no dependencies — the library has none and neither does its
suite. ~53 tests, ~9s, and the live ones skip cleanly when no mpv is found.

**The suite is characterization-first, and that framing is the point.** These
tests assert what the library *does*, warts included, not what it ought to do.
Roughly 78 repositories depend on this module, several unmaintained, so a
behaviour change reaches its victims through *their* users rather than through
this repo's issue tracker. Pinning current behaviour is what makes an
intentional change visible in review as a golden-file or assertion diff.
`tests/test_live_failure.py` exists almost entirely to be changed by the
planned error-handling work — do not "fix" its expectations to be nicer,
change them deliberately and say so in the commit.

### The two kinds of test, and why the line is where it is

* **Pure** (`test_arg_building.py`) patches `subprocess.Popen` and
  `_ipc_endpoint_ready` to read back the argv without an mpv. That is honest
  *only* because it asserts on the argv string. A stand-in that always answers
  cannot fail the way a real pipe fails.
* **Live** (`test_live_*.py`) drives a real mpv. Everything about process,
  socket and pipe lifecycle lives here, because that is the half a mock is
  structurally unable to model and where this library's real bugs are. The
  dynamic attribute machinery is built from what mpv answers at construction
  (`property-list`, `command-list`), so mocking those would test the mock.

A new test's review question is *which field of the real object did I not
model, and is that the field the test is named after?*

Two more, neither collected by `discover` (which only takes `test*.py`):

* **`tests/pipe_transport_check.py`** drives `WindowsSocket` against a
  loopback named pipe, so no mpv is started and a cycle costs a pipe rather
  than a process. It runs on any Windows interpreter — including one under
  wine, which is how the transport can be *run* while being edited from a
  Linux machine instead of first executing in CI. The embeddable zip in a
  `WINEARCH=win64` prefix is enough; no installer, no mpv. (The default
  `~/.wine` here is win32 and refuses a 64-bit `python.exe` with a misleading
  "wine32 is missing" — make a fresh prefix, don't chase that message.) It
  earned its place immediately by catching a fatal `_handle` collision that
  review had not.
* **`tests/stress_teardown.py`** repeats the full `MPV.terminate()` path
  against a real mpv, with the event handler and process supervision in
  place — the shape both real aborts arrived in, and the one a loopback pipe
  cannot model. It asserts almost nothing on purpose: the failure it hunts
  aborts the interpreter rather than raising, so the exit status is the
  assertion. It also fails on leaked threads and an unreaped mpv, both of
  which were confirmed to fire by mutation.

**Be exact about what these prove.** Under wine the first covers behaviour
and the ctypes prototypes only — it says nothing about the memory corruption,
because wine's named pipes are wine's own and the *old* PipeConnection code
also survives 2000 cycles of it. On a real Windows runner both are hunting a
fault that appeared roughly **once in 850 teardowns**, so they buy probability,
not certainty. A green stress run is never "the corruption is gone"; it is
only "no regression showed up in N tries". Say N.

### The golden API surface

`tests/api_surface.json` is a snapshot of every public class, method signature
and constant. Regenerate deliberately, in the same commit as the change:

```bash
python3 tests/test_api_surface.py --update
```

Keyword *names* are part of the contract, not just the names — downstream
passes `mpv_location=`, `quit_callback=`, `start_retries=` by keyword, so a
rename breaks callers even when the parameter still exists. The snapshot is
platform-stable by construction: `WindowsSocket` is defined unconditionally,
and `PipeConnection` is filtered out because `describe()` skips classes whose
`__module__` is not ours. That is what lets the Linux and Windows CI legs
compare the same file.

`test_python_floor.py` parses the module with `ast.parse(feature_version=...)`
against the `requires-python` floor in `pyproject.toml`. Raising that floor is
itself a downstream break (pip stops installing below it), so the floor is
fixed and the syntax is what gets checked. **It asks the interpreter whether
`feature_version` is enforced rather than assuming**: 3.9 happily accepts a
walrus at `feature_version=(3, 6)` where 3.13 rejects it, so on older legs the
check skips loudly instead of passing while proving nothing. The matrix
includes interpreters that do enforce, which is what gives the guard teeth.

Two version-portability traps, both found by the CI matrix rather than by
reading anything, and both in the *tests*:

* **The golden surface records only what each class defines** (`vars(cls)`,
  not `dir(cls)`). `dir()` drags in inherited stdlib members, and their
  introspectability moves between versions — `BaseException.with_traceback`
  reports `(self, object, /)` on 3.13 and raises on 3.11, so the golden
  disagreed with itself across the matrix while nothing in this library had
  changed. Inheritance is still recorded, in `bases`.
* **The enforcement probe above**, for the same class of reason.

### Verify a new test can fail

Green is not evidence. Break the thing the test is named after, confirm it
goes red, restore. **Restore from a file copy, never `git checkout --`** — the
working tree may hold hours of other work. All four argv mutations (`==` for
`is` in `_mpv_fmt`, dropping the underscore translation, skipping the stale
socket removal, dropping a default) were confirmed to fail the suite. And gate
on the *verdict* line (`OK` / `FAILED`), not on `Ran N tests` — truncating the
output above the verdict reads as a pass.

### Facts measured while writing these

Each of these cost a failing test to discover, so they are recorded rather
than rediscovered:

* **`--no-config=yes` is rejected by mpv.** `no-config` is not a flag option
  in its own right; pass `config=False` to get `--config=no`.
* **An idle mpv is silent at `info` and `v`.** `request_log_messages` only
  forwards what is emitted after the request, so the log-handler test needs
  `debug` or it waits out its timeout against zero messages.
* **JSON-IPC-only commands are not attached as methods.** `observe_property`,
  `unobserve_property`, `get_property`, `set_property`,
  `request_log_messages` and `client_name` are absent from mpv's
  `command-list`, so the dynamic binding never sees them. It looks like an
  omission and is the design.
* **`af` and `vf` are the real command/property collisions**, and therefore
  the live cases for the `_cmd` suffix.
* **An unavailable property reads as `None`, not an error.** `MPVInter.command`
  turns `"property unavailable"` into `None` and raises `MPVError` for
  everything else; downstream leans on this heavily (`if mpv.width:`).
* **A missing mpv binary raises `FileNotFoundError`, not `MPVError`**, so it
  escapes the retry loop on the first attempt. jellyfin-mpv-shim depends on
  that to tell "no mpv installed" from "mpv rejected an option".
* **The two transports fail differently when nothing is listening**, which the
  Windows CI leg found and reading the code did not. `UnixSocket` fails its
  `connect` and raises `OSError` immediately; `WindowsSocket` retries
  `CreateFile` five times a second apart and then raises
  `MPVError("Cannot connect to pipe.")`, which is **not** an `OSError`. So
  `except OSError:` around `MPV(start_mpv=False, ...)` is correct on Linux and
  wrong on Windows, and the Windows path also costs five seconds before it
  says so. Pinned per platform in `test_live_failure.py` rather than widened
  to `Exception`, because the divergence is the point.
* **mpv inherits the caller's stdout and stderr**, confirmed by reading
  `/proc/<mpv>/fd/1` back and finding the parent's own pipe inode.
  `close_fds` only covers fds above 2. It is invisible under `--terminal=no`
  because mpv never writes there — it costs nothing until something waits for
  **EOF** on that pipe, and then an mpv outliving `terminate()` hangs it
  forever. That is still the **default**, because callers have had mpv's
  output on their terminal for years; `discard_output=True` opts out.
* **The opt-out could not be called `quiet`.** mpv really has `--quiet`, and
  `**kwargs` forwards it, so taking that name would have silently stopped
  passing an option callers already use. Checked against `mpv --list-options`
  rather than assumed. Any future keyword of ours needs the same check.
* **`terminate()` used to return with mpv still running**, because `stop()`
  sent SIGTERM and never waited. Locally the window is under a millisecond
  with `--vo=null`, which is exactly why the orphan was intermittent in the
  wild rather than absent; real AV teardown is far slower. `stop()` now waits
  and escalates to `kill()`. The deterministic assertion is
  `process.returncode is not None` — `terminate()` alone never sets it, so
  that catches a regression without racing.
* **`EventHandler.stop(join)` passes the *flag* as `Thread.join`'s
  *timeout*.** `join=True` therefore means "wait up to 1.0 seconds" and never
  confirms the thread exited -- measured, not read. Pre-existing and left
  alone deliberately: fixing it changes what `terminate()` waits for. It is
  why `stress_teardown.py` settles before counting threads, or a loaded
  runner would fail the leak assertion for a run in which nothing leaked.
* **`join` is about joining *threads*, not about blocking**, and it must not
  gate the process wait. It is undocumented in both the docstring and
  `docs.md`, and every use of it reaches a `Thread.join` and nothing else;
  `_quit_callback` passes `False` only because a thread cannot join itself. A
  review read it as "the non-blocking teardown path" and that premise was
  simply invented -- letting it skip the wait handed the orphaned-MPV bug
  straight back to anyone who passed it. `terminate()` waits for MPV either
  way, because a `terminate()` that does not terminate is the worse failure.
* **`terminate()` re-enters itself.** Closing the socket wakes the reader into
  `_quit_callback`, which calls `terminate(join=False)` again -- so on any
  mock of the teardown path the *last* call is the internal one whatever the
  caller asked for. Assert on the first.

### CI

`.github/workflows/test.yml` — Linux (3.9/3.11/3.13) and **Windows
(3.9-3.14)**. The Windows matrix is wider on purpose: the transport rides on
Windows and interpreter behaviour rather than on anything this repo controls,
so it breaks along the *Python* axis. That is not hypothetical — the schedule
is what caught the teardown corruption twice, on 3.13 and 3.14, and the second
time on a `master` that had not changed. Hence the Monday `schedule:` run and
the `3.15-dev` leg, to hear about it before users do.
That job is deliberately **not** `continue-on-error`: it never runs on push or
pull_request, so it cannot block anyone, and GitHub notifies on a failed *run*
rather than a failed job inside a passing one — swallowing its failure would
mean an early warning nobody is ever told about. Trigger it by hand with
`gh workflow run test.yml --ref <branch>`, which works on a branch even though
the workflow is not on `master` yet. A `windows-frozen` job PyInstaller-freezes
`tests/freeze_smoke.py` and runs it against real mpv, because "works frozen,
with no dependencies" is the property this library was adopted for and no
in-process test can see it. **It `pip install .` first, and must**:
PyInstaller's analysis is static, so it cannot follow the checkout-relative
`sys.path` insert a script run from a source tree needs, and freezing without
installing produces a binary containing no library at all — which is how that
job failed the first time it ran.

A **`windows-stress`** job (scheduled/manual, 3.13/3.14/3.15-dev) hunts the
teardown corruption at volume: 50k loopback-pipe cycles, then 1.2k full
`terminate()` cycles against real mpv. It is on those interpreters because
3.13+ makes the corruption *loud* — older ones are quieter, not safer, so
stressing them would mostly buy silence. **Do not read a green run as proof.**
At ~1 abort per 850 teardowns this leg buys probability; it is a regression
detector, not a correctness proof, and its comment says so because that is the
mistake it invites.

Three things in there are load-bearing and easy to undo by accident:

* **The Linux `Install mpv` step is defended and bounded, because it hangs.**
  It has wedged more than once — not in the tests, which take 12 seconds on
  the same runner and had not started. `DEBIAN_FRONTEND: noninteractive` was
  the first fix and **did not work**: it recurred at 14m57s, which rules out
  an interactive prompt and leaves apt lock contention (the runner's own
  unattended upgrades) and stalled mirrors. Both are now defended against —
  background apt services stopped, `DPkg::Lock::Timeout`, `Acquire::Retries`,
  a per-attempt `timeout` and one retry — without needing to know which it
  was. Treat one green run as no evidence here; the failure is intermittent.
* **Every job is bounded, and each job timeout is LARGER than its steps'.**
  An unbounded GitHub job holds a runner for six hours. But a job timeout
  smaller than the sum of its step timeouts is worse than useless: the job is
  reported *cancelled* with no indication of which step stalled, which reads
  like a human pressed cancel. Steps fail by name; the job timeout is only
  the backstop. The test step's own timeout exists so a genuine test hang
  fails with the `-v` output naming the last test that started.

* **`REQUIRE_MPV: '1'`** turns "no mpv found" from a skip into a hard error. A
  mistyped `MPV_BINARY` otherwise skips every live test and the build reports
  green having tested nothing.
* **The mpv pin is `20260610`, and it is not just for reproducibility.**
  shinchiro's builds from 20260808 onward link `vulkan-1.dll` as a hard
  import; the Vulkan loader is driver-installed, so a newer mpv refuses to
  start on a CI runner and every live test fails for reasons unrelated to this
  library. jellyfin-mpv-shim pins the same build for the same regression —
  raise both together once upstream is clean. The Windows steps also *locate*
  `mpv.exe` after extraction rather than assuming a path, and fail loudly if
  it is missing.

## Docs

`docs.md` is API reference generated from docstrings (kept in the repo, not regenerated by any committed script). When changing public API, update docstrings in `python_mpv_jsonipc.py` and regenerate `docs.md` manually if needed.
