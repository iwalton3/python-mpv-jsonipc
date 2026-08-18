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
- **The Windows transport is built on two CPython internals with no compatibility contract**, and this is deliberate. `_winapi` is a private C extension; `PipeConnection` is not in `multiprocessing.connection.__all__` (which is `['Client', 'Listener', 'Pipe', 'wait']`), is defined conditionally inside `if _winapi:`, and is an implementation detail of `Pipe()` — we construct it from a handle we opened ourselves with `FILE_FLAG_OVERLAPPED`. It was chosen over `pywin32` because every native dependency costs downstream projects a PyInstaller fight (hooks, `pythoncom`/`pywintypes` DLLs, post-install registration), and it has held for six years. **Do not "tidy" these imports away** — a plain `open(r'\\.\pipe\...')` cannot do overlapped I/O, and `stop()` relies on closing the handle to break a blocked read. If they ever have to go, the replacement is `ctypes` against `kernel32` (stdlib, no hooks, PyInstaller-clean — the same property that motivated the original choice), or vendoring the ~80 lines of `_close`/`_send_bytes`/`_recv_bytes`/`_get_more_data` from CPython's `connection.py` under the PSF licence. Note this breaks on a *Python* upgrade rather than a code change, which is why the Windows CI leg needs a version matrix on a schedule, including prereleases.

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

### CI

`.github/workflows/test.yml` — Linux (3.9/3.11/3.13) and **Windows
(3.9-3.14)**. The Windows matrix is wider on purpose: `_winapi` and
`PipeConnection` break along the *Python* axis, not ours, so there is also a
Monday `schedule:` run and a `3.15-dev` leg to hear about it before users do.
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
